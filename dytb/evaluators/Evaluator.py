#Copyright (C) 2017 Paolo Galeone <nessuno@nerdz.eu>
#
#This Source Code Form is subject to the terms of the Mozilla Public
#License, v. 2.0. If a copy of the MPL was not distributed with this
#file, you can obtain one at http://mozilla.org/MPL/2.0/.
#Exhibit B is not attached; this software is compatible with the
#licenses expressed under Section 1.12 of the MPL v2.
"""Define the interface to implement to define an evaluator"""

import math
from abc import abstractproperty, ABCMeta
import tensorflow as tf
from ..inputs.interfaces import InputType
from ..models.utils import variables_to_restore


class Evaluator(object, metaclass=ABCMeta):
    """Evaluator is the class in charge of evaluate the models"""

    def __init__(self):
        self._model = None
        self._dataset = None

    @property
    def model(self):
        """Returns the model to evaluate"""
        return self._model

    @model.setter
    def model(self, model):
        """Set the model to evaluate.
        Args:
            model: implementation of the Model interface
        """
        self._model = model

    @property
    def dataset(self):
        """Returns the dataset to use to evaluate the model"""
        return self._dataset

    @dataset.setter
    def dataset(self, dataset):
        """Set the dataset to use to evaluate the model
        Args:
            dataset: implementation of the Input interface
        """
        self._dataset = dataset

    @abstractproperty
    def metrics(self):
        """Returns a list of dict with keys:
        {
            "fn": function
            "name": name
            "positive_trend_sign": sign that we like to see when things go well
            "model_selection": boolean, True if the metric has to be measured to select the model
            "average": boolean, true if the metric should be computed as average over the batches.
                       If false the results over the batches are just added
        }
        """

    def eval(self,
             metric,
             checkpoint_path,
             input_type,
             batch_size,
             augmentation_fn=None):
        """Eval the model, restoring weight found in checkpoint_path, using the dataset.
        Args:
            metric: the metric to evaluate, a single element of self.metrics
            checkpoint_path: path of the trained model checkpoint directory
            input_type: InputType enum
            batch_size: evaluate in batch of size batch_size
            augmentation_fn: if present, applies the augmentation to the input data

        Returns:
            values: list of scalar values representing the evaluation of the model on every metric,
                   on the dataset, fetching values of the specified input_type.
        """
        InputType.check(input_type)

        with tf.Graph().as_default():
            # Get inputs and targets: inputs is an input batch
            # target could be either an array of elements or a tensor.
            # it could be [label] or [label, attr1, attr2, ...]
            # or Tensor, where tensor is a standard tensorflow Tensor with
            # its own shape
            with tf.device('/cpu:0'):
                inputs, *targets = self.dataset.inputs(
                    input_type=input_type,
                    batch_size=batch_size,
                    augmentation_fn=augmentation_fn)

            # Build a Graph that computes the predictions from the
            # inference model.
            # Preditions is an array of predictions with the same cardinality of
            # targets
            _, *predictions = self._model.get(
                inputs,
                self.dataset.num_classes,
                train_phase=False,
                l2_penalty=0.0)

            if len(predictions) != len(targets):
                print(("{}.get 2nd return value and {}.inputs 2nd return "
                       "value must have the same cardinality but got: {} vs {}"
                      ).format(self._model.name, self.dataset.name,
                               len(predictions), len(targets)))
                return

            if len(predictions) == 1:
                predictions = predictions[0]
                targets = targets[0]

            metric_fn = metric["fn"](predictions, targets)

            saver = tf.train.Saver(variables_to_restore())
            with tf.Session(config=tf.ConfigProto(
                    allow_soft_placement=True)) as sess:
                ckpt = tf.train.get_checkpoint_state(checkpoint_path)
                if ckpt and ckpt.model_checkpoint_path:
                    saver.restore(sess, ckpt.model_checkpoint_path)
                else:
                    print('[!] No checkpoint file found')
                    sign = math.copysign(1, metric["positive_trend_sign"])
                    return float('inf') if sign < 0 else float("-inf")

                # Start the queue runners
                coord = tf.train.Coordinator()
                try:
                    threads = []
                    for queue_runner in tf.get_collection(
                            tf.GraphKeys.QUEUE_RUNNERS):
                        threads.extend(
                            queue_runner.create_threads(
                                sess, coord=coord, daemon=True, start=True))

                    num_iter = int(
                        math.ceil(
                            self.dataset.num_examples(input_type) / batch_size))
                    step = 0
                    metric_value_sum = 0.0
                    while step < num_iter and not coord.should_stop():
                        step += 1
                        metric_value_sum += sess.run(metric_fn)
                    avg_metric_value = metric_value_sum / step if metric[
                        "average"] else metric_value_sum
                except Exception as exc:
                    coord.request_stop(exc)
                finally:
                    coord.request_stop()

                coord.join(threads)
            return avg_metric_value

    def stats(self, checkpoint_path, batch_size, augmentation_fn=None):
        """Run the eval method on the model, see eval for arguments
        and return value description.
        Moreover, adds informations about the model and returns the whole information
        in a dictionary.
        Returns:
            dict
        """
        return {
            "train": {
                metric["name"]:
                self.eval(metric, checkpoint_path, InputType.train, batch_size,
                          augmentation_fn)
                for metric in self.metrics
            },
            "validation": {
                metric["name"]:
                self.eval(metric, checkpoint_path, InputType.validation,
                          batch_size, augmentation_fn)
                for metric in self.metrics
            },
            "test": {
                metric["name"]:
                self.eval(metric, checkpoint_path, InputType.test, batch_size,
                          augmentation_fn)
                for metric in self.metrics
            },
        }
