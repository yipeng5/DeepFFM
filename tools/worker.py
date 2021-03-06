#!/usr/bin/env python
# coding=utf-8

from __future__ import (division,absolute_import,print_function,unicode_literals)
import tensorflow as tf
import numpy as np
import argparse
import sys
import datetime
import os.path
import time
import logging
import signal
import sklearn.metrics import roc_auc_score

import _init_paths
from deepffm.data_reader import inputs
from deepffm.data_reader import load_field_range
from deepffm.model import DeepFFM

FLAGS = None

def train(server):
    logging.info('start train')
    # set train parameter
    batch_size = FLAGS.batch_size

    # Load inputs
    train_file = os.path.join(FLAGS.data_dir, 'train.tfrecords')
    test_file = os.path.join(FLAGS.data_dir, 'test.tfrecords')

    # Load field range
    field_range_path = os.path.join(FLAGS.data_dir, 'field_range.txt')
    field_range = load_field_range(field_range_path)

    worker_device = "/job:worker/task:{}/cpu:0".format(FLAGS.task_index)
    with tf.device(tf.train.replica_device_setter(1, worker_device=worker_device)):
        train_inds, train_vals, train_labels = inputs(train_file, FLAGS.batch_size, FLAGS.num_epochs)    
        test_inds, test_vals, test_labels = inputs(test_file, FLAGS.batch_size, FLAGS.num_epochs)    
        with tf.variable_scope("model"):
            deepffm = DeepFFM(field_range, embed_size=8, l2_reg_lambda = 0.00001, NUM_CLASSES=2, inds=train_inds, vals=train_vals, labels=train_labels, linear=False)
            global_step_op = tf.Variable(0, name='global_step', trainable=False)

    lr = tf.train.exponential_decay(FLAGS.lr, global_step_op, 100, 0.98, staircase = True)
    optimizer = tf.train.AdamOptimizer(lr)
    train_op = optimizer.minimize(deepffm.loss, global_step = global_step_op)

    init_all_op = tf.group(tf.global_variables_initializer(), tf.local_variables_initializer())
    merged = tf.summary.merge_all()
    variables_to_save = [v for v in tf.global_variables() if v.name.startswith("model")]
    saver = tf.train.Saver(variables_to_save)

    def init_fn(ses):
        logging.info("Initializing all parameters.")
        ses.run(init_all_op)

    config = tf.ConfigProto(device_filters=["/job:ps", "/job:worker/task:{}/cpu:0".format(FLAGS.task_index)])
    logdir = os.path.join(FLAGS.log_dir, 'model')

    train_logdir = os.path.join(FLAGS.log_dir, 'train')
    test_logdir = os.path.join(FLAGS.log_dir, 'test')

    train_writer = tf.summary.FileWriter('{}_{}'.format(train_logdir, FLAGS.task_index))
    test_writer = tf.summary.FileWriter('{}_{}'.format(test_logdir, FLAGS.task_index))

    sv = tf.train.Supervisor(is_chief=(FLAGS.task_index == 0),
                             logdir=logdir,
                             saver=saver,
                             summary_op=None,
                             init_op=init_all_op,
                             init_fn=init_fn,
                             ready_op=tf.report_uninitialized_variables(variables_to_save),
                             summary_writer=train_writer,
                             global_step=global_step_op,
                             )

    with sv.managed_session(server.target, config=config) as sess, sess.as_default():
        global_step = sess.run(global_step_op)
        sess.run(init_all_op)
        try:
            step = 0
            while not sv.should_stop() :
                # Train data
                if step % 10 == 0:
                    start_time = time.time()
                    _, summary, loss_value, accuracy, auc, lr_value, global_step, logits, labels = sess.run([train_op, merged, deepffm.loss, deepffm.accuracy, deepffm.auc, lr, global_step_op, deepffm.logits[:,1], deepffm.labels_])
                    sk_auc = roc_auc_score(labels, logits)

                    duration = time.time() - start_time
                    logging.info('Step %d, Global %d, : loss = %.5f, accuracy = %.5f, auc = %.5f, sk_auc = %.5f, lr = %.5f.(%.5f sec)' \
                            % (step, global_step, loss_value, accuracy, auc, sk_auc, lr_value, duration))

                else:
                    _, summary = sess.run([train_op, merged])
                train_writer.add_summary(summary, global_step)

                # Test data
                if step % 100 == 0:
                    deepffm.inds, deepffm.vals, deepffm.labels = [test_inds, test_vals, test_labels]
                    start_time = time.time()
                    summary, loss_value, accuracy, auc, logits, labels = sess.run([merged, deepffm.loss, deepffm.accuracy, deepffm.auc, deepffm.logits[:,1], deepffm.labels_])
                    sk_auc = roc_auc_score(labels, logits)

                    duration = time.time() - start_time
                    logging.info('\tTest Step %d, Global %d, : loss = %.5f, accuracy = %.5f, auc = %.5f, sk_auc = %.5f. (%.5f sec)' \
                            % (step, global_step, loss_value, accuracy, auc, sk_auc, duration))

                    test_writer.add_summary(summary, global_step)
                    deepffm.inds, deepffm.vals, deepffm.labels = [train_inds, train_vals, train_labels]

                #logging.info('sv should stop: {}'.format(sv.should_stop()))
                step += 1

        except tf.errors.OutOfRangeError:
            logging.info('Done training for %d epochs, %d steps.' % (FLAGS.num_epochs, step))
        finally:
            sv.stop()
            logging.info('reached %s steps. worker stopped.', global_step)
            train_writer.close()
            test_writer.close()

def cluster_spec(num_workers, num_ps):
    """ More tensorflow setup for data parallelism """
    cluster = {}
    port = 12222

    all_ps = []
    host = '127.0.0.1'
    for _ in range(num_ps):
        all_ps.append('{}:{}'.format(host, port))
        port += 1
    cluster['ps'] = all_ps

    all_workers = []
    for _ in range(num_workers):
        all_workers.append('{}:{}'.format(host, port))
        port += 1
    cluster['worker'] = all_workers
    return cluster

def main(_):
    now = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")

    #logging.basicConfig(level=logging.INFO, filename=os.path.join(FLAGS.log_dir, now+'.log'))
    logging.basicConfig(level=logging.INFO)

    spec = cluster_spec(FLAGS.num_workers, 1)
    cluster = tf.train.ClusterSpec(spec).as_cluster_def()

    def shutdown(signal, frame):
        logging.warn('Received signal %s: exiting', signal)
        sys.exit(128 + signal)

    signal.signal(signal.SIGHUP, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    if FLAGS.job_name == "worker":
        server = tf.train.Server(cluster, job_name="worker", task_index=FLAGS.task_index,
                                 config=tf.ConfigProto(intra_op_parallelism_threads=1, inter_op_parallelism_threads=2))
        train(server)

    elif FLAGS.job_name == "ps":
        server = tf.train.Server(cluster, job_name="ps", task_index=FLAGS.task_index,
                                 config=tf.ConfigProto(device_filters=["/job:ps"]))
        server.join()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--num_epochs', type=int, default=100,
                      help='Number of epochs to run trainer.')
    parser.add_argument('--lr', type=float, default=0.01,
                      help='Initial learning rate')
    parser.add_argument('--data_dir', type=str, default='/home/wing/DataSet/criteo/pre/deepffm/downSample',
                      help='Directory for storing input data')
    parser.add_argument('--log_dir', type=str, default='/home/wing/Project/DeepFFM/logs',
                      help='Summaries log directory')
    parser.add_argument('--batch_size', default=1000, type=int, help='Batch size')
    parser.add_argument('--job_name', default="worker", help='worker or ps')
    parser.add_argument('--task_index', default=0, type=int, help='Task index')
    parser.add_argument('--num_workers', default=1, type=int, help='Number of workers')
    FLAGS = parser.parse_args()
    tf.app.run(main=main, argv=[sys.argv[0]])
