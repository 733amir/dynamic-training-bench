#Copyright (C) 2016 Paolo Galeone <nessuno@nerdz.eu>
#
#This Source Code Form is subject to the terms of the Mozilla Public
#License, v. 2.0. If a copy of the MPL was not distributed with this
#file, you can obtain one at http://mozilla.org/MPL/2.0/.
#Exhibit B is not attached; this software is compatible with the
#licenses expressed under Section 1.12 of the MPL v2.
"""Dynamically define the train bench via CLI"""

import sys
from datetime import datetime
import os.path
import time
import math

import numpy as np
import tensorflow as tf
import evaluate
from inputs.utils import InputType
from models.utils import variables_to_save, tf_log, MODEL_SUMMARIES
from models.utils import put_kernels_on_grid
from models.Autoencoder import Autoencoder
from models.Classifier import Classifier
from CLIArgs import CLIArgs


def build_optimizer(global_step):
    """Build the CLI specified optimizer, log the learning rate and enalble
    learning rate decay is specified.
    Args:
        global_step: integer tensor, the current training step
    Returns:
        optimizer: tf.Optimizer object initialized
    """
    # Extract the initial learning rate
    initial_lr = float(ARGS.optimizer_args['learning_rate'])

    if ARGS.lr_decay:
        # Decay the learning rate exponentially based on the number of steps.
        steps_per_decay = STEPS_PER_EPOCH * ARGS.lr_decay_epochs
        learning_rate = tf.train.exponential_decay(
            initial_lr,
            global_step,
            steps_per_decay,
            ARGS.lr_decay_factor,
            staircase=True)
        # Update the learning rate parameter of the optimizer
        ARGS.optimizer_args['learning_rate'] = learning_rate
    else:
        learning_rate = tf.constant(initial_lr)

    # Log the learning rate
    tf_log(tf.summary.scalar('learning_rate', learning_rate))

    # Instantiate the optimizer
    optimizer = getattr(tf.train, ARGS.optimizer)(**ARGS.optimizer_args)
    return optimizer


def classifier():
    """Trains the classifier, returns the best validation accuracy reached
    and saves the best model (with the highest validation accuracy).

    Returns:
        best_va: best validation accuracy"""

    best_va = 0.0

    with tf.Graph().as_default(), tf.device(ARGS.train_device):
        global_step = tf.Variable(0, trainable=False, name='global_step')

        # Get images and labels
        images, labels = DATASET.distorted_inputs(ARGS.batch_size)
        log_io(images)
        # Build a Graph that computes the logits predictions from the
        # inference model.
        is_training_, logits = MODEL.get(images,
                                         DATASET.num_classes(),
                                         train_phase=True,
                                         l2_penalty=ARGS.l2_penalty)

        # Calculate loss.
        loss = MODEL.loss(logits, labels)
        tf_log(tf.summary.scalar('loss', loss))

        # Create optimizer and log learning rate
        optimizer = build_optimizer(global_step)
        train_op = optimizer.minimize(loss, global_step=global_step)

        # Create the train saver.
        train_saver, best_saver = build_savers([global_step])

        # Train accuracy ops
        with tf.variable_scope('accuracy'):
            top_k_op = tf.nn.in_top_k(logits, labels, 1)
            train_accuracy = tf.reduce_mean(tf.cast(top_k_op, tf.float32))
            # General validation summary
            accuracy_value_ = tf.placeholder(tf.float32, shape=())
            accuracy_summary = tf.summary.scalar('accuracy', accuracy_value_)

        # read collection after that every op added its own
        # summaries in the train_summaries collection
        train_summaries = tf.summary.merge(
            tf.get_collection_ref(MODEL_SUMMARIES))

        # Build an initialization operation to run below.
        init = tf.variables_initializer(tf.global_variables() +
                                        tf.local_variables())

        # Start running operations on the Graph.
        with tf.Session(config=tf.ConfigProto(
                allow_soft_placement=True)) as sess:
            sess.run(init)

            # Start the queue runners with a coordinator
            coord = tf.train.Coordinator()
            threads = tf.train.start_queue_runners(sess=sess, coord=coord)

            if not ARGS.restart:  # continue from the saved checkpoint
                # restore previous session if exists
                checkpoint = tf.train.latest_checkpoint(LOG_DIR)
                if checkpoint:
                    train_saver.restore(sess, checkpoint)
                else:
                    print('[I] Unable to restore from checkpoint')

            train_log, validation_log = build_loggers(sess.graph)

            # Extract previous global step value
            old_gs = sess.run(global_step)

            # Restart from where we were
            for step in range(old_gs, MAX_STEPS):
                start_time = time.time()
                _, loss_value = sess.run([train_op, loss],
                                         feed_dict={is_training_: True})
                duration = time.time() - start_time

                if np.isnan(loss_value):
                    print('Model diverged with loss = NaN')
                    break

                # update logs every 10 iterations
                if step % 10 == 0:
                    examples_per_sec = ARGS.batch_size / duration
                    sec_per_batch = float(duration)

                    format_str = ('{}: step {}, loss = {:.4f} '
                                  '({:.1f} examples/sec; {:.3f} sec/batch)')
                    print(
                        format_str.format(datetime.now(), step, loss_value,
                                          examples_per_sec, sec_per_batch))
                    # log train values
                    summary_lines = sess.run(train_summaries,
                                             feed_dict={is_training_: True})
                    train_log.add_summary(summary_lines, global_step=step)

                # Save the model checkpoint at the end of every epoch
                # evaluate train and validation performance
                if (step > 0 and
                        step % STEPS_PER_EPOCH == 0) or (step + 1) == MAX_STEPS:
                    checkpoint_path = os.path.join(LOG_DIR, 'model.ckpt')
                    train_saver.save(sess, checkpoint_path, global_step=step)

                    # validation accuracy
                    va_value = eval_model(LOG_DIR, InputType.validation)

                    summary_line = sess.run(
                        accuracy_summary, feed_dict={accuracy_value_: va_value})
                    validation_log.add_summary(summary_line, global_step=step)

                    # train accuracy
                    ta_value = sess.run(train_accuracy,
                                        feed_dict={is_training_: False})
                    summary_line = sess.run(
                        accuracy_summary, feed_dict={accuracy_value_: ta_value})
                    train_log.add_summary(summary_line, global_step=step)

                    print(
                        '{} ({}): train accuracy = {:.3f} validation accuracy = {:.3f}'.
                        format(datetime.now(),
                               int(step / STEPS_PER_EPOCH), ta_value, va_value))
                    # save best model
                    if va_value > best_va:
                        best_va = va_value
                        best_saver.save(
                            sess,
                            os.path.join(BEST_MODEL_DIR, 'model.ckpt'),
                            global_step=step)
            # end of for

            validation_log.close()
            train_log.close()

            # When done, ask the threads to stop.
            coord.request_stop()
            # Wait for threads to finish.
            coord.join(threads)
    return best_va


def autoencoder():
    """Train the autoencoder, returns the best validation error reached
    and saves the best model (with the lower validation error).

    Returns:
        best_ve: best validation error"""

    best_ve = float('inf')
    with tf.Graph().as_default(), tf.device(ARGS.train_device):
        global_step = tf.Variable(0, trainable=False, name='global_step')

        # Get images and discard labels
        images, _ = DATASET.distorted_inputs(ARGS.batch_size)

        # Build a Graph that computes the reconstructions predictions from the
        # inference model.
        is_training_, reconstructions = MODEL.get(images,
                                                  train_phase=True,
                                                  l2_penalty=ARGS.l2_penalty)

        log_io(images, reconstructions)

        # Calculate loss.
        loss = MODEL.loss(reconstructions, images)
        # reconstruction error
        error_ = tf.placeholder(tf.float32, shape=())
        error = tf.summary.scalar('error', error_)

        # Create optimizer and log learning rate
        optimizer = build_optimizer(global_step)
        # Training op
        train_op = optimizer.minimize(loss, global_step=global_step)

        # Create the savers
        train_saver, best_saver = build_savers([global_step])

        # read collection after that every op added its own
        # summaries in the train_summaries collection
        train_summaries = tf.summary.merge(
            tf.get_collection_ref(MODEL_SUMMARIES))

        # Build an initialization operation to run below.
        init = tf.variables_initializer(tf.global_variables() +
                                        tf.local_variables())

        # Start running operations on the Graph.
        with tf.Session(config=tf.ConfigProto(
                allow_soft_placement=True)) as sess:
            sess.run(init)

            # Start the queue runners with a coordinator
            coord = tf.train.Coordinator()
            threads = tf.train.start_queue_runners(sess=sess, coord=coord)

            if not ARGS.restart:  # continue from the saved checkpoint
                # restore previous session if exists
                checkpoint = tf.train.latest_checkpoint(LOG_DIR)
                if checkpoint:
                    train_saver.restore(sess, checkpoint)
                else:
                    print('[I] Unable to restore from checkpoint')

            train_log, validation_log = build_loggers(sess.graph)

            # Extract previous global step value
            old_gs = sess.run(global_step)

            # Restart from where we were
            for step in range(old_gs, MAX_STEPS):
                start_time = time.time()
                _, loss_value = sess.run([train_op, loss],
                                         feed_dict={is_training_: True})
                duration = time.time() - start_time

                if np.isnan(loss_value):
                    print('Model diverged with loss = NaN')
                    break

                # update logs every 10 iterations
                if step % 10 == 0:
                    num_examples_per_step = ARGS.batch_size
                    examples_per_sec = num_examples_per_step / duration
                    sec_per_batch = float(duration)

                    format_str = ('{}: step {}, loss = {:.4f} '
                                  '({:.1f} examples/sec; {:.3f} sec/batch)')
                    print(
                        format_str.format(datetime.now(), step, loss_value,
                                          examples_per_sec, sec_per_batch))
                    # log train error and summaries
                    train_error_summary_line, train_summary_line = sess.run(
                        [error, train_summaries],
                        feed_dict={error_: loss_value,
                                   is_training_: True})
                    train_log.add_summary(
                        train_error_summary_line, global_step=step)
                    train_log.add_summary(train_summary_line, global_step=step)

                # Save the model checkpoint at the end of every epoch
                # evaluate train and validation performance
                if (step > 0 and
                        step % STEPS_PER_EPOCH == 0) or (step + 1) == MAX_STEPS:
                    checkpoint_path = os.path.join(LOG_DIR, 'model.ckpt')
                    train_saver.save(sess, checkpoint_path, global_step=step)

                    # validation error
                    ve_value = eval_model(LOG_DIR, InputType.validation)

                    summary_line = sess.run(error, feed_dict={error_: ve_value})
                    validation_log.add_summary(summary_line, global_step=step)

                    print('{} ({}): train error = {} validation error = {}'.
                          format(datetime.now(
                          ), int(step / STEPS_PER_EPOCH), loss_value, ve_value))
                    if ve_value < best_ve:
                        best_ve = ve_value
                        best_saver.save(
                            sess,
                            os.path.join(BEST_MODEL_DIR, 'model.ckpt'),
                            global_step=step)
            # end of for

            validation_log.close()
            train_log.close()

            # When done, ask the threads to stop.
            coord.request_stop()
            # Wait for threads to finish.
            coord.join(threads)
    return best_ve


def log_io(inputs, outputs=None):
    """Log inputs and outputs batch of images.
    Args:
        inputs: tensor with shape [Batch_size, height, widht, depth]
        outputs: if present must be the same dimensions as inputs
    """
    with tf.variable_scope('visualization'):
        grid_side = math.floor(math.sqrt(ARGS.batch_size))
        inputs = put_kernels_on_grid(
            tf.transpose(
                inputs, perm=(1, 2, 3, 0))[:, :, :, 0:grid_side**2],
            grid_side)

        if outputs is None:
            tf_log(tf.summary.image('inputs', inputs, max_outputs=1))
            return

        inputs = tf.pad(inputs, [[0, 0], [0, 0], [0, 10], [0, 0]])
        outputs = put_kernels_on_grid(
            tf.transpose(
                outputs, perm=(1, 2, 3, 0))[:, :, :, 0:grid_side**2],
            grid_side)
        tf_log(
            tf.summary.image(
                'input_output', tf.concat(2, [inputs, outputs]), max_outputs=1))


def build_savers(variables_to_add):
    """Add variables_to_add to the collection of variables to save.
    Returns:
        train_saver: saver to use to log the training model
        best_saver: saver used to save the best model
    """
    variables = variables_to_save(variables_to_add)
    train_saver = tf.train.Saver(variables, max_to_keep=2)
    best_saver = tf.train.Saver(variables, max_to_keep=1)
    return train_saver, best_saver


def build_loggers(graph):
    """Build the FileWriter object used to log summaries.
    Args:
        the graph which operations to log refers to
    Returns:
        train_log: tf.summary.FileWriter object to log train op
        validation_log: tf.summary.FileWriter object to log validation op
    """
    train_log = tf.summary.FileWriter(
        os.path.join(LOG_DIR, 'train'), graph=graph)
    validation_log = tf.summary.FileWriter(
        os.path.join(LOG_DIR, 'validation'), graph=graph)
    return train_log, validation_log


def eval_model(checkpoint_dir, input_type):
    """Execute the proper evalutation of the MODEL, using the model
    found in checkpoint_dir, using the specified input_tyoe
    Args:
        model: the model to evaluate
        input_type: the Type.inputType enum that defines the input
    Returns:
        val: the evaluation results
    """
    if not isinstance(input_type, InputType):
        raise ValueError("Invalid input_type, required a valid InputType")

    if isinstance(MODEL, Classifier):
        return evaluate.accuracy(
            checkpoint_dir, MODEL, DATASET, input_type, device=ARGS.eval_device)
    if isinstance(MODEL, Autoencoder):
        return evaluate.error(
            checkpoint_dir, MODEL, DATASET, input_type, device=ARGS.eval_device)
    raise ValueError("Evaluate method not defined for this model type")


def train():
    """Execute the right training proceudre for the current MODEL"""
    if isinstance(MODEL, Classifier):
        return classifier()
    if isinstance(MODEL, Autoencoder):
        return autoencoder()
    raise ValueError("train method not defined for this model type")


if __name__ == '__main__':
    ARGS, NAME, MODEL, DATASET = CLIArgs().parse_train()

    #### Training constants ####
    STEPS_PER_EPOCH = math.ceil(
        DATASET.num_examples(InputType.train) / ARGS.batch_size)
    MAX_STEPS = STEPS_PER_EPOCH * ARGS.epochs

    #### Model logs and checkpoint constants ####
    CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
    LOG_DIR = os.path.join(CURRENT_DIR, 'log', ARGS.model, NAME)
    BEST_MODEL_DIR = os.path.join(LOG_DIR, 'best')

    #### Dataset and logs ####
    DATASET.maybe_download_and_extract()

    if tf.gfile.Exists(LOG_DIR) and ARGS.restart:
        tf.gfile.DeleteRecursively(LOG_DIR)
    tf.gfile.MakeDirs(LOG_DIR)
    if not tf.gfile.Exists(BEST_MODEL_DIR):
        tf.gfile.MakeDirs(BEST_MODEL_DIR)

    # Start train and get the best value for the metric
    VALIDATION_METRIC = train()

    # Save the best error value on the validation set
    with open(os.path.join(CURRENT_DIR, 'validation_results.txt'), 'a') as res:
        res.write('{} {}: {} {}\n'.format(datetime.now(), ARGS.model, NAME,
                                          VALIDATION_METRIC))

    # Use the 'best' model to calculate the error on the test set
    with open(os.path.join(CURRENT_DIR, 'test_results.txt'), 'a') as res:
        res.write('{} {}: {} {}\n'.format(datetime.now(
        ), ARGS.model, NAME, eval_model(BEST_MODEL_DIR, InputType.test)))
    sys.exit()
