# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Attention-based decoder functions."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
import tensorflow as tf

from tensorflow.contrib.layers.python.layers import layers
from tensorflow.python.ops import rnn_cell_impl
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import function
from tensorflow.python.framework import ops
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import control_flow_ops
from tensorflow.python.ops import tensor_array_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import nn_ops
from tensorflow.python.ops import variable_scope
from tensorflow.python.util import nest

__all__ = ["prepare_attention",
           "attention_decoder_fn_train",
           "attention_decoder_fn_inference"
]


def attention_decoder_fn_train(encoder_state,
                               attention_keys,
                               attention_values,
                               attention_score_fn,
                               attention_construct_fn,
                               output_alignments=False,
                               max_length=None,
                               name=None):
    """Attentional decoder function for `dynamic_rnn_decoder` during training.

    The `attention_decoder_fn_train` is a training function for an
    attention-based sequence-to-sequence model. It should be used when
    `dynamic_rnn_decoder` is in the training mode.

    The `attention_decoder_fn_train` is called with a set of the user arguments
    and returns the `decoder_fn`, which can be passed to the
    `dynamic_rnn_decoder`, such that

    ```
    dynamic_fn_train = attention_decoder_fn_train(encoder_state)
    outputs_train, state_train = dynamic_rnn_decoder(
            decoder_fn=dynamic_fn_train, ...)
    ```

    Further usage can be found in the `kernel_tests/seq2seq_test.py`.

    Args:
        encoder_state: The encoded state to initialize the `dynamic_rnn_decoder`.
        attention_keys: to be compared with target states.
        attention_values: to be used to construct context vectors.
        attention_score_fn: to compute similarity between key and target states.
        attention_construct_fn: to build attention states.
        name: (default: `None`) NameScope for the decoder function;
            defaults to "simple_decoder_fn_train"

    Returns:
        A decoder function with the required interface of `dynamic_rnn_decoder`
        intended for training.
    """
    with ops.name_scope(name, "attention_decoder_fn_train", [
            encoder_state, attention_keys, attention_values, attention_score_fn,
            attention_construct_fn
    ]):
        pass

    def decoder_fn(time, cell_state, cell_input, cell_output, context_state):
        """Decoder function used in the `dynamic_rnn_decoder` for training.

        Args:
            time: positive integer constant reflecting the current timestep.
            cell_state: state of RNNCell.
            cell_input: input provided by `dynamic_rnn_decoder`.
            cell_output: output of RNNCell.
            context_state: context state provided by `dynamic_rnn_decoder`.

        Returns:
            A tuple (done, next state, next input, emit output, next context state)
            where:

            done: `None`, which is used by the `dynamic_rnn_decoder` to indicate
            that `sequence_lengths` in `dynamic_rnn_decoder` should be used.

            next state: `cell_state`, this decoder function does not modify the
            given state.

            next input: `cell_input`, this decoder function does not modify the
            given input. The input could be modified when applying e.g. attention.

            emit output: `cell_output`, this decoder function does not modify the
            given output.

            next context state: `context_state`, this decoder function does not
            modify the given context state. The context state could be modified when
            applying e.g. beam search.
        """
        with ops.name_scope(
                name, "attention_decoder_fn_train",
                [time, cell_state, cell_input, cell_output, context_state]):

            if cell_state is None:  # first call, return encoder_state
                cell_state = encoder_state
                # init attention
                attention = _init_attention(encoder_state)
                if output_alignments:
                    context_state = tensor_array_ops.TensorArray(dtype=dtypes.float32,
                                                                 tensor_array_name="alignments_ta",
                                                                 size=max_length,
                                                                 dynamic_size=True, infer_shape=False)
            else:  # construct attention
                attention = attention_construct_fn(cell_output, attention_keys, attention_values)
                if output_alignments:
                    attention, alignments = attention
                    context_state = context_state.write(time-1, alignments)
                cell_output = attention

            # combine cell_input and attention
            next_input = array_ops.concat([cell_input, attention], 1)

            return (None, cell_state, next_input, cell_output, context_state)

    return decoder_fn


def attention_decoder_fn_inference(output_fn,
                                   encoder_state,
                                   attention_keys,
                                   attention_values,
                                   attention_score_fn,
                                   attention_construct_fn,
                                   embeddings,
                                   start_of_sequence_id,
                                   end_of_sequence_id,
                                   maximum_length,
                                   num_decoder_symbols,
                                   dtype=dtypes.int32,
                                   selector_fn=None,
                                   imem=None,
                                   name=None):
    """Attentional decoder function for `dynamic_rnn_decoder` during inference.

    The `attention_decoder_fn_inference` is a simple inference function for a
    sequence-to-sequence model. It should be used when `dynamic_rnn_decoder` is
    in the inference mode.

    The `attention_decoder_fn_inference` is called with user arguments
    and returns the `decoder_fn`, which can be passed to the
    `dynamic_rnn_decoder`, such that

    ```
    dynamic_fn_inference = attention_decoder_fn_inference(...)
    outputs_inference, state_inference = dynamic_rnn_decoder(
            decoder_fn=dynamic_fn_inference, ...)
    ```

    Further usage can be found in the `kernel_tests/seq2seq_test.py`.

    Args:
        output_fn: An output function to project your `cell_output` onto class
        logits.

        An example of an output function;

        ```
            tf.variable_scope("decoder") as varscope
                output_fn = lambda x: layers.linear(x, num_decoder_symbols,
                                                                                        scope=varscope)

                outputs_train, state_train = seq2seq.dynamic_rnn_decoder(...)
                logits_train = output_fn(outputs_train)

                varscope.reuse_variables()
                logits_inference, state_inference = seq2seq.dynamic_rnn_decoder(
                        output_fn=output_fn, ...)
        ```

        If `None` is supplied it will act as an identity function, which
        might be wanted when using the RNNCell `OutputProjectionWrapper`.

        encoder_state: The encoded state to initialize the `dynamic_rnn_decoder`.
        attention_keys: to be compared with target states.
        attention_values: to be used to construct context vectors.
        attention_score_fn: to compute similarity between key and target states.
        attention_construct_fn: to build attention states.
        embeddings: The embeddings matrix used for the decoder sized
        `[num_decoder_symbols, embedding_size]`.
        start_of_sequence_id: The start of sequence ID in the decoder embeddings.
        end_of_sequence_id: The end of sequence ID in the decoder embeddings.
        maximum_length: The maximum allowed of time steps to decode.
        num_decoder_symbols: The number of classes to decode at each time step.
        dtype: (default: `dtypes.int32`) The default data type to use when
        handling integer objects.
        name: (default: `None`) NameScope for the decoder function;
            defaults to "attention_decoder_fn_inference"

    Returns:
        A decoder function with the required interface of `dynamic_rnn_decoder`
        intended for inference.
    """
    with ops.name_scope(name, "attention_decoder_fn_inference", [
        output_fn, encoder_state, attention_keys, attention_values,
        attention_score_fn, attention_construct_fn, embeddings,
        start_of_sequence_id, end_of_sequence_id, maximum_length,
        num_decoder_symbols, dtype, selector_fn, imem,
    ]):
        start_of_sequence_id = ops.convert_to_tensor(start_of_sequence_id, dtype)
        end_of_sequence_id = ops.convert_to_tensor(end_of_sequence_id, dtype)
        maximum_length = ops.convert_to_tensor(maximum_length, dtype)
        num_decoder_symbols = ops.convert_to_tensor(num_decoder_symbols, dtype)
        encoder_info = nest.flatten(encoder_state)[0]
        batch_size = encoder_info.get_shape()[0].value
        if output_fn is None:
            output_fn = lambda x: x
        if batch_size is None:
            batch_size = array_ops.shape(encoder_info)[0]

    def decoder_fn(time, cell_state, cell_input, cell_output, context_state):
        """Decoder function used in the `dynamic_rnn_decoder` for inference.

        The main difference between this decoder function and the `decoder_fn` in
        `attention_decoder_fn_train` is how `next_cell_input` is calculated. In
        decoder function we calculate the next input by applying an argmax across
        the feature dimension of the output from the decoder. This is a
        greedy-search approach. (Bahdanau et al., 2014) & (Sutskever et al., 2014)
        use beam-search instead.

        Args:
            time: positive integer constant reflecting the current timestep.
            cell_state: state of RNNCell.
            cell_input: input provided by `dynamic_rnn_decoder`.
            cell_output: output of RNNCell.
            context_state: context state provided by `dynamic_rnn_decoder`.

        Returns:
            A tuple (done, next state, next input, emit output, next context state)
            where:

            done: A boolean vector to indicate which sentences has reached a
            `end_of_sequence_id`. This is used for early stopping by the
            `dynamic_rnn_decoder`. When `time>=maximum_length` a boolean vector with
            all elements as `true` is returned.

            next state: `cell_state`, this decoder function does not modify the
            given state.

            next input: The embedding from argmax of the `cell_output` is used as
            `next_input`.

            emit output: If `output_fn is None` the supplied `cell_output` is
            returned, else the `output_fn` is used to update the `cell_output`
            before calculating `next_input` and returning `cell_output`.

            next context state: `context_state`, this decoder function does not
            modify the given context state. The context state could be modified when
            applying e.g. beam search.

        Raises:
            ValueError: if cell_input is not None.

        """
        with ops.name_scope(
                name, "attention_decoder_fn_inference",
                [time, cell_state, cell_input, cell_output, context_state]):
            if cell_input is not None:
                raise ValueError("Expected cell_input to be None, but saw: %s" % cell_input)

            if cell_output is None:
                # invariant that this is time == 0
                next_input_id = array_ops.ones(
                        [batch_size,], dtype=dtype) * (start_of_sequence_id)
                done = array_ops.zeros([batch_size,], dtype=dtypes.bool)
                cell_state = encoder_state
                cell_output = array_ops.zeros(
                        [num_decoder_symbols], dtype=dtypes.float32)
                word_input = array_ops.gather(embeddings, next_input_id)
                naf_triple_id = array_ops.zeros([batch_size, 2], dtype=dtype)
                triple_input = array_ops.gather_nd(imem[1], naf_triple_id)
                cell_input = array_ops.concat([word_input, triple_input], axis=1)
                # init attention
                attention = _init_attention(encoder_state)
                if imem is not None:   # added by our model
                    context_state = tensor_array_ops.TensorArray(dtype=dtypes.int32,
                                                                 tensor_array_name="output_ids_ta",
                                                                 size=maximum_length,
                                                                 dynamic_size=True,
                                                                 infer_shape=False)
            else:
                # construct attention
                attention = attention_construct_fn(cell_output, attention_keys, attention_values)

                if type(attention) is tuple:  # added by our model
                    attention, alignment = attention[0], attention[1]
                    cell_output = attention
                    alignment = tf.reshape(alignment, [batch_size, -1])
                    selector = selector_fn(cell_output)
                    selector = tf.sigmoid(selector)
                    logit = output_fn(cell_output)
                    # cell output
                    cell_output = logit

                    word_prob = nn_ops.softmax(logit) * (1 - selector)
                    entity_prob = alignment * selector
                    mask = array_ops.reshape(math_ops.cast(math_ops.greater(tf.reduce_max(word_prob, 1),
                                                                            tf.reduce_max(entity_prob, 1)),
                                                           dtype=dtypes.float32), [-1,1])
                    word_input = mask * array_ops.gather(
                        embeddings,
                        math_ops.cast(
                            math_ops.argmax(word_prob, 1), dtype=dtype)) + (1 - mask) * array_ops.gather_nd(
                        imem[0], array_ops.concat([array_ops.reshape(
                            math_ops.range(batch_size, dtype=dtype), [-1,1]),
                            array_ops.reshape(math_ops.cast(math_ops.argmax(entity_prob, 1), dtype=dtype), [-1,1])],
                            axis=1))
                    indices = array_ops.concat(
                        [array_ops.reshape(math_ops.range(batch_size, dtype=dtype), [-1,1]),
                         math_ops.cast(1-mask, dtype=dtype) * tf.reshape(
                             math_ops.cast(math_ops.argmax(alignment, 1), dtype=dtype), [-1, 1])],
                        axis=1)
                    triple_input = array_ops.gather_nd(imem[1], indices)

                    # cell input
                    cell_input = array_ops.concat([word_input, triple_input], axis=1)

                    # done mask
                    mask = array_ops.reshape(math_ops.cast(mask, dtype=dtype), [-1])
                    input_id = mask * math_ops.cast(
                        math_ops.argmax(word_prob, 1), dtype=dtype) +\
                               (mask - 1) * math_ops.cast(math_ops.argmax(entity_prob, 1), dtype=dtype)
                    context_state = context_state.write(time-1, input_id)
                    done = array_ops.reshape(math_ops.equal(input_id, end_of_sequence_id), [-1])
                else:
                    cell_output = attention
                    # argmax decoder
                    cell_output = output_fn(cell_output)    # logits
                    next_input_id = math_ops.cast(
                            math_ops.argmax(cell_output, 1), dtype=dtype)
                    done = math_ops.equal(next_input_id, end_of_sequence_id)
                    cell_input = array_ops.gather(embeddings, next_input_id)

            # combine cell_input and attention
            next_input = array_ops.concat([cell_input, attention], 1)

            # if time > maxlen, return all true vector
            done = control_flow_ops.cond(
                    math_ops.greater(time, maximum_length),
                    lambda: array_ops.ones([batch_size,], dtype=dtypes.bool),
                    lambda: done)
            return (done, cell_state, next_input, cell_output, context_state)

    return decoder_fn


# Helper functions ##
def prepare_attention(attention_states,
                      attention_option,
                      num_units,
                      scope_name=None,
                      imem=None,
                      output_alignments=False,
                      reuse=False):
    """Prepare keys/values/functions for attention.
    Args:
        attention_states: hidden states to attend over.
        attention_option: how to compute attention, either "luong" or "bahdanau".
        num_units: hidden state dimension.
        reuse: whether to reuse variable scope.
    Returns:
        attention_keys: to be compared with target states.
        attention_values: to be used to construct context vectors.
        attention_score_fn: to compute similarity between key and target states.
        attention_construct_fn: to build attention states.
    """

    # Prepare attention keys / values from attention_states
    if type(attention_states) is tuple:
        with variable_scope.variable_scope("%s/attn_enc" % scope_name, reuse=reuse) as scope:
            attention_keys_enc = layers.linear(attention_states[0], num_units,
                                               biases_initializer=None, scope=scope)
            attention_values_enc = attention_states[0]
        with variable_scope.variable_scope("%s/attn_dec" % scope_name, reuse=reuse) as scope:
            attention_keys_dec, attention_values_dec = array_ops.split(
                layers.linear(attention_states[1],
                              num_units * 2, biases_initializer=None, scope=scope),
                [num_units, num_units], axis=2)

        if imem is not None:  # added by our model
            if type(imem) is tuple:
                with variable_scope.variable_scope("%s/imem_graph" % scope_name, reuse=reuse) as scope:
                    attention_keys_graph, attention_values_graph = array_ops.split(layers.linear(
                        imem[0], num_units * 2, biases_initializer=None, scope=scope), [num_units, num_units], axis=2)
                with variable_scope.variable_scope("%s/imem_triple" % scope_name, reuse=reuse) as scope:
                    attention_keys_triple, attention_values_triple = array_ops.split(layers.linear(
                        imem[1], num_units * 2, biases_initializer=None, scope=scope), [num_units, num_units], axis=3)
            else:
                raise ValueError("Expected imem to be type tuple.")
            # concat as attention as (ctx_e, ctx_c, g, k)
            attention_keys = (attention_keys_enc, attention_keys_dec,
                              attention_keys_graph, attention_keys_triple)
            attention_values = (attention_values_enc, attention_values_dec,
                                attention_values_graph, attention_values_triple)
            attention_score_fn = (_create_attention_score_fn("%s/attention_enc_score" % scope_name, num_units,
                                                             attention_option, reuse),
                                  _create_attention_score_fn("%s/attention_dec_score" % scope_name, num_units,
                                                             attention_option, reuse),
                                  _create_attention_score_fn("%s/imem_score" % scope_name, num_units,
                                                             "luong", reuse, output_alignments=output_alignments))
        else:
            attention_keys = (attention_keys_enc, attention_keys_dec)
            attention_values = (attention_values_enc, attention_values_dec)
            attention_score_fn = (_create_attention_score_fn("%s/attention_enc_score" % scope_name, num_units,
                                                             attention_option, reuse),
                                  _create_attention_score_fn("%s/attention_dec_score" % scope_name, num_units,
                                                             attention_option, reuse))
    else:
        with variable_scope.variable_scope("%s/attention_keys" % scope_name, reuse=reuse) as scope:
            attention_keys = layers.linear(attention_states, num_units,
                                           biases_initializer=None, scope=scope)
            attention_values = attention_states
            attention_score_fn = _create_attention_score_fn("%s/attention_score" % scope_name, num_units,
                                                            attention_option, reuse)
        if imem is not None:  # added by our model
            if type(imem) is tuple:
                with variable_scope.variable_scope("%s/imem_graph" % scope_name, reuse=reuse) as scope:
                    attention_keys_graph, attention_values_graph = array_ops.split(layers.linear(
                        imem[0], num_units * 2, biases_initializer=None, scope=scope), [num_units, num_units], axis=2)
                with variable_scope.variable_scope("%s/imem_triple" % scope_name, reuse=reuse) as scope:
                    attention_keys_triple, attention_values_triple = array_ops.split(layers.linear(
                        imem[1], num_units * 2, biases_initializer=None, scope=scope), [num_units, num_units], axis=3)
            else:
                raise ValueError("Expected imem to be type tuple.")
            # concat as attention as (c, g, k)
            attention_keys = (attention_keys, attention_keys_graph, attention_keys_triple)
            attention_values = (attention_values, attention_values_graph, attention_values_triple)
            attention_score_fn = (_create_attention_score_fn("%s/attention_score" % scope_name, num_units,
                                                             attention_option, reuse),
                                  _create_attention_score_fn("%s/imem_score" % scope_name, num_units,
                                                             "luong", reuse, output_alignments=output_alignments))

    # Attention construction function
    attention_construct_fn = _create_attention_construct_fn("%s/attention_construct" % scope_name,
                                                            num_units, attention_score_fn, reuse)

    return attention_keys, attention_values, attention_score_fn, attention_construct_fn


def _init_attention(encoder_state):
    """Initialize attention. Handling both LSTM and GRU.
    Args:
        encoder_state: The encoded state to initialize the `dynamic_rnn_decoder`.
    Returns:
        attn: initial zero attention vector.
    """

    # Multi- vs single-layer
    # TODO(thangluong): is this the best way to check?
    if isinstance(encoder_state, tuple):
        top_state = encoder_state[-1]
    else:
        top_state = encoder_state

    # LSTM vs GRU
    if isinstance(top_state, rnn_cell_impl.LSTMStateTuple):
        attn = array_ops.zeros_like(top_state.h)
    else:
        attn = array_ops.zeros_like(top_state)

    return attn


def _create_attention_construct_fn(name, num_units, attention_score_fn, reuse):
    """Function to compute attention vectors.
    Args:
        name: to label variables.
        num_units: hidden state dimension.
        attention_score_fn: to compute similarity between key and target states.
        reuse: whether to reuse variable scope.
    Returns:
        attention_construct_fn: to build attention states.
    """
    with variable_scope.variable_scope(name, reuse=reuse) as scope:

        def construct_fn(attention_query, attention_keys, attention_values):
            alignments = None
            if len(attention_keys) == 4:  # attend (context_enc, context_dec, context_graph, context_triple)
                context_enc = attention_score_fn[0](attention_query, attention_keys[0], attention_values[0])
                context_dec = attention_score_fn[1](attention_query, attention_keys[1], attention_values[1])
                context_graph, context_triple, alignments = attention_score_fn[2](attention_query,
                                                                                  attention_keys[2:],
                                                                                  attention_values[2:])
                concat_input = array_ops.concat([attention_query, context_enc, context_dec,
                                                 context_graph, context_triple], 1)
                attention = layers.linear(concat_input, num_units, biases_initializer=None, scope=scope)
                print("scope2:", attention.name)
            elif len(attention_keys) == 3:  # attend (context_enc, context_graph, context_triple)
                context_enc = attention_score_fn[0](attention_query, attention_keys[0], attention_values[0])
                context_graph, context_triple, alignments = attention_score_fn[1](attention_query,
                                                                                  attention_keys[1:],
                                                                                  attention_values[1:])
                concat_input = array_ops.concat([attention_query, context_enc,
                                                 context_graph, context_triple], 1)
                attention = layers.linear(concat_input, num_units, biases_initializer=None, scope=scope)
                print("scope1:", attention.name)
            elif len(attention_keys) == 2:  # attend (context_enc, context_dec)
                context_enc = attention_score_fn[0](attention_query, attention_keys[0], attention_values[0])
                context_dec = attention_score_fn[1](attention_query, attention_keys[1], attention_values[1])
                concat_input = array_ops.concat([attention_query, context_enc, context_dec], 1)
                attention = layers.linear(concat_input, num_units, biases_initializer=None, scope=scope)
            else:  # just attend context
                context = attention_score_fn(attention_query, attention_keys, attention_values)
                concat_input = array_ops.concat([attention_query, context], 1)
                attention = layers.linear(concat_input, num_units, biases_initializer=None, scope=scope)

            if alignments is None:
                return attention
            else:  # added by our model
                return attention, alignments

        return construct_fn


def _create_attention_score_fn(name, num_units, attention_option, reuse,
                               output_alignments=False, dtype=dtypes.float32):
    """Different ways to compute attention scores.
    Args:
        name: to label variables.
        num_units: hidden state dimension.
        attention_option: how to compute attention, either "luong" or "bahdanau".
            "bahdanau": additive (Bahdanau et al., ICLR'2015)
            "luong": multiplicative (Luong et al., EMNLP'2015)
        reuse: whether to reuse variable scope.
        dtype: (default: `dtypes.float32`) data type to use.
    Returns:
        attention_score_fn: to compute similarity between key and target states.
    """
    with variable_scope.variable_scope(name, reuse=reuse):
        if attention_option == "bahdanau":
            query_w = variable_scope.get_variable("attnW", [num_units, num_units], dtype=dtype)
            score_v = variable_scope.get_variable("attnV", [num_units], dtype=dtype)

        def attention_score_fn(query, keys, values):
            """Put attention masks on attention_values using attention_keys and query.
            Args:
                query: A Tensor of shape [batch_size, num_units].
                keys: A Tensor of shape [batch_size, attention_length, num_units].
                values: A Tensor of shape [batch_size, attention_length, num_units].
            Returns:
                context_vector: A Tensor of shape [batch_size, num_units].
            Raises:
                ValueError: if attention_option is neither "luong" or "bahdanau".
            """
            triple_keys, triple_values = None, None
            if type(keys) is tuple:  # use knowledge attention
                keys, triple_keys = keys
                values, triple_values = values

            if attention_option == "bahdanau":
                # transform query
                query = math_ops.matmul(query, query_w)

                # reshape query: [batch_size, 1, num_units]
                query = array_ops.reshape(query, [-1, 1, num_units])

                # attn_fun
                scores = _attn_add_fun(score_v, keys, query)
            elif attention_option == "luong":
                # reshape query: [batch_size, 1, num_units]
                query = array_ops.reshape(query, [-1, 1, num_units])

                # attn_fun
                scores = _attn_mul_fun(keys, query)
            else:
                raise ValueError("Unknown attention option %s!" % attention_option)

            # Compute alignment weights
            #     scores: [batch_size, length]
            #     alignments: [batch_size, length]
            # TODO(thangluong): not normalize over padding positions.
            alignments = nn_ops.softmax(scores)

            # Now calculate the attention-weighted vector.
            new_alignments = array_ops.expand_dims(alignments, 2)
            context_vector = math_ops.reduce_sum(new_alignments * values, [1])
            context_vector.set_shape([None, num_units])

            if triple_values is not None:  # use knowledge attention
                triple_scores = math_ops.reduce_sum(triple_keys * array_ops.reshape(query, [-1, 1, 1, num_units]), [3])
                triple_alignments = nn_ops.softmax(triple_scores)
                context_triples = math_ops.reduce_sum(array_ops.expand_dims(triple_alignments, 3) * triple_values, [2])
                context_graph_triples = math_ops.reduce_sum(new_alignments * context_triples, [1])
                context_graph_triples.set_shape([None, num_units])
                final_alignments = new_alignments * triple_alignments
                return context_vector, context_graph_triples, final_alignments
            else:
                if output_alignments:
                    return context_vector, alignments
                else:
                    return context_vector

        return attention_score_fn


# keys: [batch_size, attention_length, attn_size]
# query: [batch_size, 1, attn_size]
# return weights [batch_size, attention_length]
@function.Defun(func_name="attn_add_fun", noinline=True)
def _attn_add_fun(v, keys, query):
    return math_ops.reduce_sum(v * math_ops.tanh(keys + query), [2])


@function.Defun(func_name="attn_mul_fun", noinline=True)
def _attn_mul_fun(keys, query):
    return math_ops.reduce_sum(keys * query, [2])
