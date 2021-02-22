#!/usr/bin/env python
#coding=utf-8
"""
TensorFlow Implementation of <<DeepFM: A Factorization-Machine based Neural Network for CTR Prediction>> with the fellowing features：
#1 Input pipline using Dataset high level API, Support parallel and prefetch reading
#2 Train pipline using Coustom Estimator by rewriting model_fn
#3 Support distincted training using TF_CONFIG
#4 Support export_model for TensorFlow Serving

by lambdaji
"""
#from __future__ import absolute_import
#from __future__ import division
#from __future__ import print_function

#import argparse
import shutil
#import sys
import os
import json
import glob
from datetime import date, timedelta
from time import time
#import gc
#from multiprocessing import Process

#import math
import random
#import pandas as pd
#import numpy as np
import tensorflow as tf
#liangaws: 导入horovod库
import horovod.tensorflow as hvd 

#liangaws: 为了使用Sagemaker的pipe mode，需要导入下面的库
from sagemaker_tensorflow import PipeModeDataset
from tensorflow.contrib.data import map_and_batch

#################### CMD Arguments ####################
FLAGS = tf.app.flags.FLAGS
tf.app.flags.DEFINE_integer("feature_size", 0, "Number of features")
tf.app.flags.DEFINE_integer("field_size", 0, "Number of fields")
tf.app.flags.DEFINE_integer("embedding_size", 32, "Embedding size")
tf.app.flags.DEFINE_integer("num_epochs", 10, "Number of epochs")
tf.app.flags.DEFINE_integer("batch_size", 64, "Number of batch size")
tf.app.flags.DEFINE_integer("log_steps", 1000, "save summary every steps")
tf.app.flags.DEFINE_float("learning_rate", 0.0005, "learning rate")
tf.app.flags.DEFINE_float("l2_reg", 0.0001, "L2 regularization")
tf.app.flags.DEFINE_string("loss_type", 'log_loss', "loss type {square_loss, log_loss}")
tf.app.flags.DEFINE_string("optimizer", 'Adam', "optimizer type {Adam, Adagrad, GD, Momentum}")
tf.app.flags.DEFINE_string("deep_layers", '256,128,64', "deep layers")
tf.app.flags.DEFINE_string("dropout", '0.5,0.5,0.5', "dropout rate")
tf.app.flags.DEFINE_boolean("batch_norm", False, "perform batch normaization (True or False)")
tf.app.flags.DEFINE_float("batch_norm_decay", 0.9, "decay for the moving average(recommend trying decay=0.9)")
tf.app.flags.DEFINE_string("training_data_dir", '', "training data dir")
tf.app.flags.DEFINE_string("val_data_dir", '', "validation data dir")
tf.app.flags.DEFINE_string("model_dir", '', "model checkpoint dir")
tf.app.flags.DEFINE_string("servable_model_dir", '', "export servable model for TensorFlow Serving")
tf.app.flags.DEFINE_string("task_type", 'train', "task type {train, infer, eval, export}")
tf.app.flags.DEFINE_boolean("clear_existing_model", False, "clear existing model or not")

tf.app.flags.DEFINE_list("hosts", json.loads(os.environ.get('SM_HOSTS')), "get the all cluster instances name for distribute training")
tf.app.flags.DEFINE_string("current_host", os.environ.get('SM_CURRENT_HOST'), "get current execute the program host name")
tf.app.flags.DEFINE_integer("pipe_mode", 0, "sagemaker data input pipe mode")
tf.app.flags.DEFINE_integer("worker_per_host", 1, "worker process per training instance")
tf.app.flags.DEFINE_string("training_channel_name", '', "training channel name for input_fn")
tf.app.flags.DEFINE_string("evaluation_channel_name", '', "evaluation channel name for input_fn")
tf.app.flags.DEFINE_boolean("enable_s3_shard", False, "whether enable S3 shard(True or False), this impact whether do dataset shard in input_fn")
tf.app.flags.DEFINE_boolean("enable_data_multi_path", False, "whether use different dataset path for each channel, this impact how to do dataset shard(ONLY apply for Pipe mode) in input_fn")

#end of liangaws

#1 1:0.5 2:0.03519 3:1 4:0.02567 7:0.03708 8:0.01705 9:0.06296 10:0.18185 11:0.02497 12:1 14:0.02565 15:0.03267 17:0.0247 18:0.03158 20:1 22:1 23:0.13169 24:0.02933 27:0.18159 31:0.0177 34:0.02888 38:1 51:1 63:1 132:1 164:1 236:1
#liangaws: 为了使用sagemaker的pipe mode，给input_fn增加了一个channel参数
def input_fn(filenames='', channel='training', batch_size=32, num_epochs=1, perform_shuffle=False):
    
    def decode_tfrecord(batch_examples):
        # The feature definition here should BE consistent with LibSVM TO TFRecord process.
        features = tf.parse_example(batch_examples,
                                           features={
                                               "label": tf.FixedLenFeature([], tf.float32),
                                               "ids": tf.FixedLenFeature(dtype=tf.int64, shape=[FLAGS.field_size]),
                                               "values": tf.FixedLenFeature(dtype=tf.float32, shape=[FLAGS.field_size]) 
                                           })
        
        batch_label = features["label"]
        batch_ids = features["ids"]
        batch_values = features["values"]
        
        return {"feat_ids": batch_ids, "feat_vals": batch_values}, batch_label

    # Extract lines from input files using the Dataset API, can pass one filename or filename list
    if FLAGS.pipe_mode == 0:
        dataset = tf.data.TFRecordDataset(filenames) 

        if FLAGS.enable_s3_shard : #ShardedByS3Key
            dataset = dataset.shard(FLAGS.worker_per_host, hvd.local_rank())
        else : #S3FullReplicate
            dataset = dataset.shard(hvd.size(), hvd.rank())
                  
#         if perform_shuffle:  #shishuai ?? 
#             dataset = dataset.shuffle(buffer_size=1024*1024)
    
    else :
        print("-------enter into pipe mode branch!------------")
        dataset = PipeModeDataset(channel, record_format='TFRecord')
        
        number_host = len(FLAGS.hosts)
        #liangaws: horovod + pipe mode下，如果每个训练实例有多个worker，需要每个worker对应一个不同的channel，因此建议每个channel中的数据集是提前经过切分好的。只要在多个训练实例上并且每个训练实例是多个worker进程的情况下，才需要对不同训练实例上的同一个channel的数据做shard。
        
        if FLAGS.enable_data_multi_path : 
            if FLAGS.enable_s3_shard == False :
                if number_host > 1:
                    #liangaws: 在Sagemaker horovod方式下，不同训练实例的current-host都是一样的
                    index = hvd.rank() // FLAGS.worker_per_host
                    dataset = dataset.shard(number_host, index)
        else :
            if FLAGS.enable_s3_shard :
                dataset = dataset.shard(FLAGS.worker_per_host, hvd.local_rank())
            else :
                dataset = dataset.shard(hvd.size(), hvd.rank())
     
    dataset = dataset.batch(batch_size, drop_remainder=True) # Batch size to use
    dataset = dataset.map(decode_tfrecord,
                        num_parallel_calls=tf.data.experimental.AUTOTUNE)
    
    dataset = dataset.cache() # shishuai ??

    if num_epochs > 1:
        dataset = dataset.repeat(num_epochs)

    dataset = dataset.prefetch(buffer_size=tf.data.experimental.AUTOTUNE)

    return dataset

#shishaui ??
#     iterator = dataset.make_one_shot_iterator() 
#     batch_features, batch_labels = iterator.get_next()

#     return batch_features, batch_labels
    
def model_fn(features, labels, mode, params):
    """Bulid Model function f(x) for Estimator."""
    #------hyperparameters----
    field_size = params["field_size"]
    feature_size = params["feature_size"]
    embedding_size = params["embedding_size"]
    l2_reg = params["l2_reg"]
    # liangaws: 使用Horovod的时候需要scale learning rate by the number of workers.
    learning_rate = params["learning_rate"] * hvd.size()
    #batch_norm_decay = params["batch_norm_decay"]
    #optimizer = params["optimizer"]
    
    #liangaws: 作者其实使用python2来实现的，但是Sagemaker的tensorflow容器当前只能用python3.所以下面的代码把map对象修改为list对象。
    layers  = list(map(int, params["deep_layers"].split(',')))
    dropout = list(map(float, params["dropout"].split(',')))

    #------bulid weights------
    FM_B = tf.get_variable(name='fm_bias', shape=[1], initializer=tf.constant_initializer(0.0))
    FM_W = tf.get_variable(name='fm_w', shape=[feature_size], initializer=tf.glorot_normal_initializer())
    FM_V = tf.get_variable(name='fm_v', shape=[feature_size, embedding_size], initializer=tf.glorot_normal_initializer())

    #------build feaure-------
    feat_ids  = features['feat_ids']
    feat_ids = tf.reshape(feat_ids,shape=[-1,field_size])
    feat_vals = features['feat_vals']
    feat_vals = tf.reshape(feat_vals,shape=[-1,field_size])

    #------build f(x)------
    with tf.variable_scope("First-order"):
        feat_wgts = tf.nn.embedding_lookup(FM_W, feat_ids)              # None * F * 1
        y_w = tf.reduce_sum(tf.multiply(feat_wgts, feat_vals),1)

    with tf.variable_scope("Second-order"):
        embeddings = tf.nn.embedding_lookup(FM_V, feat_ids)             # None * F * K
        feat_vals = tf.reshape(feat_vals, shape=[-1, field_size, 1])
        embeddings = tf.multiply(embeddings, feat_vals)                 #vij*xi
        sum_square = tf.square(tf.reduce_sum(embeddings,1))
        square_sum = tf.reduce_sum(tf.square(embeddings),1)
        y_v = 0.5*tf.reduce_sum(tf.subtract(sum_square, square_sum),1)	# None * 1

    with tf.variable_scope("Deep-part"):
        if FLAGS.batch_norm:
            #normalizer_fn = tf.contrib.layers.batch_norm
            #normalizer_fn = tf.layers.batch_normalization
            if mode == tf.estimator.ModeKeys.TRAIN:
                train_phase = True
                #normalizer_params = {'decay': batch_norm_decay, 'center': True, 'scale': True, 'updates_collections': None, 'is_training': True, 'reuse': None}
            else:
                train_phase = False
                #normalizer_params = {'decay': batch_norm_decay, 'center': True, 'scale': True, 'updates_collections': None, 'is_training': False, 'reuse': True}
        else:
            normalizer_fn = None
            normalizer_params = None

        deep_inputs = tf.reshape(embeddings,shape=[-1,field_size*embedding_size]) # None * (F*K)
        
   
        for i in range(len(layers)):
            #if FLAGS.batch_norm:
            #    deep_inputs = batch_norm_layer(deep_inputs, train_phase=train_phase, scope_bn='bn_%d' %i)
                #normalizer_params.update({'scope': 'bn_%d' %i})
            
            
            deep_inputs = tf.contrib.layers.fully_connected(inputs=deep_inputs, num_outputs=layers[i], \
                #normalizer_fn=normalizer_fn, normalizer_params=normalizer_params, \
                weights_regularizer=tf.contrib.layers.l2_regularizer(l2_reg), scope='mlp%d' % i)
            if FLAGS.batch_norm:
                deep_inputs = batch_norm_layer(deep_inputs, train_phase=train_phase, scope_bn='bn_%d' %i)   #放在RELU之后 https://github.com/ducha-aiki/caffenet-benchmark/blob/master/batchnorm.md#bn----before-or-after-relu
            if mode == tf.estimator.ModeKeys.TRAIN:
                deep_inputs = tf.nn.dropout(deep_inputs, keep_prob=dropout[i])                              #Apply Dropout after all BN layers and set dropout=0.8(drop_ratio=0.2)
                #deep_inputs = tf.layers.dropout(inputs=deep_inputs, rate=dropout[i], training=mode == tf.estimator.ModeKeys.TRAIN)

        y_deep = tf.contrib.layers.fully_connected(inputs=deep_inputs, num_outputs=1, activation_fn=tf.identity, \
                weights_regularizer=tf.contrib.layers.l2_regularizer(l2_reg), scope='deep_out')
        y_d = tf.reshape(y_deep,shape=[-1])
        #sig_wgts = tf.get_variable(name='sigmoid_weights', shape=[layers[-1]], initializer=tf.glorot_normal_initializer())
        #sig_bias = tf.get_variable(name='sigmoid_bias', shape=[1], initializer=tf.constant_initializer(0.0))
        #deep_out = tf.nn.xw_plus_b(deep_inputs,sig_wgts,sig_bias,name='deep_out')

    with tf.variable_scope("DeepFM-out"):
        #y_bias = FM_B * tf.ones_like(labels, dtype=tf.float32)  # None * 1  warning;这里不能用label，否则调用predict/export函数会出错，train/evaluate正常；初步判断estimator做了优化，用不到label时不传
        y_bias = FM_B * tf.ones_like(y_d, dtype=tf.float32)      # None * 1
        y = y_bias + y_w + y_v + y_d
        pred = tf.sigmoid(y)

    predictions={"prob": pred}
    export_outputs = {tf.saved_model.signature_constants.DEFAULT_SERVING_SIGNATURE_DEF_KEY: tf.estimator.export.PredictOutput(predictions)}
    # Provide an estimator spec for `ModeKeys.PREDICT`
    if mode == tf.estimator.ModeKeys.PREDICT:
        return tf.estimator.EstimatorSpec(
                mode=mode,
                predictions=predictions,
                export_outputs=export_outputs)

    #------bulid loss------
    loss = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(logits=y, labels=labels)) + \
        l2_reg * tf.nn.l2_loss(FM_W) + \
        l2_reg * tf.nn.l2_loss(FM_V)

    # Provide an estimator spec for `ModeKeys.EVAL`
    eval_metric_ops = {
        "auc": tf.metrics.auc(labels, pred)
    }
    if mode == tf.estimator.ModeKeys.EVAL:
        return tf.estimator.EstimatorSpec(
                mode=mode,
                predictions=predictions,
                loss=loss,
                eval_metric_ops=eval_metric_ops)

    #------bulid optimizer------
    if FLAGS.optimizer == 'Adam':
        optimizer = tf.train.AdamOptimizer(learning_rate=learning_rate, beta1=0.9, beta2=0.999, epsilon=1e-8)
    elif FLAGS.optimizer == 'Adagrad':
        optimizer = tf.train.AdagradOptimizer(learning_rate=learning_rate, initial_accumulator_value=1e-8)
    elif FLAGS.optimizer == 'Momentum':
        optimizer = tf.train.MomentumOptimizer(learning_rate=learning_rate, momentum=0.95)
    elif FLAGS.optimizer == 'ftrl':
        optimizer = tf.train.FtrlOptimizer(learning_rate)

    #liangaws: 利用Horovod训练的时候，用Horovod Distributed Optimizer对原始optimizer进行wrapper。
    optimizer = hvd.DistributedOptimizer(optimizer)
    train_op = optimizer.minimize(loss, global_step=tf.train.get_global_step())
    
    #train_op = optimizer.minimize(loss, global_step=tf.train.get_or_create_global_step())
         
    # Provide an estimator spec for `ModeKeys.TRAIN` modes
    if mode == tf.estimator.ModeKeys.TRAIN:
        return tf.estimator.EstimatorSpec(
                mode=mode,
                predictions=predictions,
                loss=loss,
                train_op=train_op)

    # Provide an estimator spec for `ModeKeys.EVAL` and `ModeKeys.TRAIN` modes.
    #return tf.estimator.EstimatorSpec(
    #        mode=mode,
    #        loss=loss,
    #        train_op=train_op,
    #        predictions={"prob": pred},
    #        eval_metric_ops=eval_metric_ops)

def batch_norm_layer(x, train_phase, scope_bn):
    bn_train = tf.contrib.layers.batch_norm(x, decay=FLAGS.batch_norm_decay, center=True, scale=True, updates_collections=None, is_training=True,  reuse=None, scope=scope_bn)
    bn_infer = tf.contrib.layers.batch_norm(x, decay=FLAGS.batch_norm_decay, center=True, scale=True, updates_collections=None, is_training=False, reuse=True, scope=scope_bn)
    z = tf.cond(tf.cast(train_phase, tf.bool), lambda: bn_train, lambda: bn_infer)
    return z

def main(_):
    #liangaws:测试sagemaker传入python程序的参数。
    import sys
    print(sys.argv)
    
    #liangaws: initialize Horovod.
    hvd.init()
    
    #------check Arguments------
    print('task_type ', FLAGS.task_type)
    print('model_dir ', FLAGS.model_dir)
    print('training_data_dir ', FLAGS.training_data_dir)
    print('val_data_dir ', FLAGS.val_data_dir)
    print('num_epochs ', FLAGS.num_epochs)
    print('feature_size ', FLAGS.feature_size)
    print('field_size ', FLAGS.field_size)
    print('embedding_size ', FLAGS.embedding_size)
    print('batch_size ', FLAGS.batch_size)
    print('deep_layers ', FLAGS.deep_layers)
    print('dropout ', FLAGS.dropout)
    print('loss_type ', FLAGS.loss_type)
    print('optimizer ', FLAGS.optimizer)
    print('learning_rate ', FLAGS.learning_rate)
    print('batch_norm_decay ', FLAGS.batch_norm_decay)
    print('batch_norm ', FLAGS.batch_norm)
    print('l2_reg ', FLAGS.l2_reg)

    #------init Envs------
    #liangaws: 这里利用glob.glob函数可以把data_dir目录下的所有训练文件名抽取出来组成一个list，之后可以直接把这个文件名list传给TextLineDataset。 
    #for tfrecord file
    #liangaws: for tfrecord file
    if FLAGS.pipe_mode == 0:
        tr_files = glob.glob(r"%s/**/tr*.tfrecords" % FLAGS.training_data_dir, recursive=True)
        random.shuffle(tr_files)
        va_files = glob.glob(r"%s/**/va*.tfrecords" % FLAGS.val_data_dir, recursive=True)
        te_files = glob.glob(r"%s/**/te*.tfrecords" % FLAGS.val_data_dir, recursive=True)
    else :
        tr_files = ''
        va_files = ''
        te_files = ''
        
    print("tr_files:", tr_files)
    print("va_files:", va_files)
    print("te_files:", te_files)
       
    if FLAGS.clear_existing_model:
        try:
            shutil.rmtree(FLAGS.model_dir)
        except Exception as e:
            print(e, "at clear_existing_model")
        else:
            print("existing model cleaned at %s" % FLAGS.model_dir)

    #------bulid Tasks------
    model_params = {
        "field_size": FLAGS.field_size,
        "feature_size": FLAGS.feature_size,
        "embedding_size": FLAGS.embedding_size,
        "learning_rate": FLAGS.learning_rate,
        "batch_norm_decay": FLAGS.batch_norm_decay,
        "l2_reg": FLAGS.l2_reg,
        "deep_layers": FLAGS.deep_layers,
        "dropout": FLAGS.dropout
    }

    #liangaws: 使用Horovod， pin GPU to be used to process local rank (one GPU per process)    
    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True
    config.gpu_options.visible_device_list = str(hvd.local_rank())
       
    # liangaws: 使用Horovod的时候， save checkpoints only on worker 0 to prevent other workers from corrupting them.
    print('current horovod rank is ', hvd.rank())
    print('input model dir is ', FLAGS.model_dir)
    
    print("host is ", FLAGS.hosts)
    print('current host is ', FLAGS.current_host)
    
    
    if hvd.rank() == 0:
        DeepFM = tf.estimator.Estimator(model_fn=model_fn, model_dir=FLAGS.model_dir, params=model_params, config=tf.estimator.RunConfig().replace(session_config=config))
    else:
        DeepFM = tf.estimator.Estimator(model_fn=model_fn, model_dir=None, params=model_params, config=tf.estimator.RunConfig().replace(session_config=config))
    
    
    # liangaws: 使用Horovod的时候， BroadcastGlobalVariablesHook broadcasts initial variable states from rank 0 to all other processes. This is necessary to ensure consistent initialization of all workers when training is started with random weights or restored from a checkpoint.
    bcast_hook = hvd.BroadcastGlobalVariablesHook(0)
    
    #liangaws: 为了在Sagemaker pipe mode下使用horovod的单机多个worker进程，需要在调用Sagemaker的estimator fit的时候用多个channel，至少单机的每个worker需要一个channel。从SM设置的环境变量SM_CHANNELS可以获得当前的所有channel名字，之后每个worker用单独的channel来进行数据读取。
    #这里channel名字的顺序与调用Sagemaker estimator fit时候写入的顺序是不同的。比如对于{'training':train_s3, 'training-2':train2_s3, 'evaluation': validate_s3}这样的三个channel，环境变量被SM设置为['evaluation', 'training', 'training-2']，也就是说最后一个channel 'evaluation'出现在环境变量SM_CHANNELS中的第一个，其他channel则是按照原来顺序排列。
    channel_names = json.loads(os.environ['SM_CHANNELS'])
    print("channel name", channel_names)
    print("first channel", channel_names[0])
    print("last channel name", channel_names[-1])
    eval_channel=channel_names[0]
        
    if FLAGS.task_type == 'train':
        #liangaws:增加hook到TrainSpec中
        """
        train_spec = tf.estimator.TrainSpec(input_fn=lambda: input_fn(tr_files, channel='training', num_epochs=FLAGS.num_epochs, batch_size=FLAGS.batch_size), hooks=[bcast_hook])
        eval_spec = tf.estimator.EvalSpec(input_fn=lambda: input_fn(va_files, channel='evaluation', num_epochs=1, batch_size=FLAGS.batch_size), steps=None, start_delay_secs=1000, throttle_secs=1200)
        tf.estimator.train_and_evaluate(DeepFM, train_spec, eval_spec)
        
        """
        if FLAGS.pipe_mode == 0: #file mode
            for _ in range(FLAGS.num_epochs): # shishuai ??
                DeepFM.train(input_fn=lambda: input_fn(tr_files, num_epochs=1, batch_size=FLAGS.batch_size), hooks=[bcast_hook])
                if hvd.rank() == 0:  #只需要在horovod的master做模型评估
                    DeepFM.evaluate(input_fn=lambda: input_fn(va_files, num_epochs=1, batch_size=FLAGS.batch_size))
        else :  #pipe mode
            #liangaws: horovod + pipe mode方式下，训练中worker第二次进入input_fn中的时候，继续使用PipeModeDataset对同一个FIFO读取数据会出问题。
            """
            train_spec = tf.estimator.TrainSpec(input_fn=lambda: input_fn(channel=channel_names[1 + hvd.local_rank()], num_epochs=FLAGS.num_epochs, batch_size=FLAGS.batch_size), hooks=[bcast_hook])
            eval_spec = tf.estimator.EvalSpec(input_fn=lambda: input_fn(channel=eval_channel, num_epochs=1, batch_size=FLAGS.batch_size), steps=None, start_delay_secs=1000, throttle_secs=1200)
            tf.estimator.train_and_evaluate(DeepFM, train_spec, eval_spec)
        
            """
            DeepFM.train(input_fn=lambda: input_fn(channel=channel_names[1 + hvd.local_rank()], num_epochs=FLAGS.num_epochs, batch_size=FLAGS.batch_size), hooks=[bcast_hook])
            if hvd.rank() == 0:  #只需要在horovod的master做模型评估
                DeepFM.evaluate(input_fn=lambda: input_fn(channel=eval_channel, num_epochs=1, batch_size=FLAGS.batch_size))
            
        
    elif FLAGS.task_type == 'eval':
        DeepFM.evaluate(input_fn=lambda: input_fn(va_files, num_epochs=1, batch_size=FLAGS.batch_size))
    elif FLAGS.task_type == 'infer':
        preds = DeepFM.predict(input_fn=lambda: input_fn(te_files, num_epochs=1, batch_size=FLAGS.batch_size), predict_keys="prob")
        with open(FLAGS.val_data_dir+"/pred.txt", "w") as fo:
            for prob in preds:
                fo.write("%f\n" % (prob['prob']))
    #liangaws:这里修改当任务类型是train或者export的时候都保存模型
    if FLAGS.task_type == 'export' or FLAGS.task_type == 'train': 
        #feature_spec = tf.feature_column.make_parse_example_spec(feature_columns)
        #feature_spec = {
        #    'feat_ids': tf.FixedLenFeature(dtype=tf.int64, shape=[None, FLAGS.field_size]),
        #    'feat_vals': tf.FixedLenFeature(dtype=tf.float32, shape=[None, FLAGS.field_size])
        #}
        #serving_input_receiver_fn = tf.estimator.export.build_parsing_serving_input_receiver_fn(feature_spec)
        feature_spec = {
            'feat_ids': tf.placeholder(dtype=tf.int64, shape=[None, FLAGS.field_size], name='feat_ids'),
            'feat_vals': tf.placeholder(dtype=tf.float32, shape=[None, FLAGS.field_size], name='feat_vals')
        }
        serving_input_receiver_fn = tf.estimator.export.build_raw_serving_input_receiver_fn(feature_spec)
        
        #liangaws: 使用Horovod的时候: Save model and history only on worker 0 (i.e. master)
        if hvd.rank() == 0:
            DeepFM.export_savedmodel(FLAGS.servable_model_dir,
                                     serving_input_receiver_fn)

if __name__ == "__main__":
    tf.logging.set_verbosity(tf.logging.INFO)
    tf.app.run()
