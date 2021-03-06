# -*- coding: utf-8 -*-
import sys
import theano
# Automatically detect we are debugging and turn on maximal Theano debugging
debug = sys.gettrace() is not None
if debug:
    print("Debugging")
    theano.config.optimizer = 'fast_compile'  # or "None"
    theano.config.exception_verbosity = 'high'
    theano.config.compute_test_value = 'warn'

from blocks.bricks.recurrent import BaseRecurrent, recurrent
from blocks.bricks import Initializable, Linear
from blocks.bricks.base import application
from blocks.bricks.parallel import Fork, Parallel

class RecurrentStack(BaseRecurrent, Initializable):
    u"""Stack of recurrent networks.

    Build a stack of recurrent layers from a supplied list of
    BaseRecurrent objects. Each object must have a `sequences`,
    `contexts`, `states` and `outputs` parameters to its `apply` method,
    such as the ones required by the recurrent decorator from
    :mod:`blocks.bricks.recurrent`.

    The apply parameters from all layers are concatenated.
    In order to avoid conflict, the names of the elements appearing in the
    `states` and `outputs` of the apply method of each layers
    are renamed. The names of the bottom layer are used as-is and for the
    layers above it a suffixed of the form '_<n>' is added, where '<n>' is
    the number of the layer starting from 1 (for first layer above bottom)

    The `contexts` of all layers are merged into a single list of unique
    names, and no suffix is added. Different layers with the same context
    name will receive the same value.

    The names that appear in `sequences` are treated in the same way
    if `skip_connections` is set to True.
    The only exception is the "mask" element that may appear in the
    `sequences` parameter of all layers, no suffix is added to it and
    all layers will receive the same mask value.
    If you set `skip_connections` to False then only the elements of the
    `sequences` from the bottom layer will appear in the `sequences`
    parameter of the apply method of this class.
    When using this class you can supply all inputs to all layers using
    a single fork which is created with `output_names` set to the
    `apply.sequences` of this class' object.

    Whether or not `skip_connections` is set, each layer above the bottom
    also receive an input (value to its `sequences`) from a fork
    of the state of the layer below it.
    It is assumed that all `states` have a "states" element
    (this can be configured with `states_name` parameter.) This element
    is forked and then added to all the elements appearing in the
    `sequences` of the next layer (except for "mask".) If
    `skip_connections` is False then this fork has a bias by default.
    This allows direct usage of this class with input supplied only to the
    first layer. But if you do supply inputs to all layers then there is
    no bias and the fork you use to supply the inputs should have its own
    separate bias.

    Parameters
    ----------
    transitions : list
        List of recurrent units to use in each layer.
        Note: A suffix with layer number is added to transitions' names.
    fork_prototype : :class:`.FeedForward`, optional
        A prototype for the  transformation applied to states_name from
        the states of each layer. The transformation is used when state
        is forked to the sequences of the next layer. By default a
         :class:`.Linear` transformation is used, with bias if
         skip_connections is False.
    states_name : string
        In a stack of RNN the state of each layer is used as input to the
        next. The `states_name` identify the element of the states of each
        layer that should be used for this task. By default the element is
        called "states". To be more precise, this is the name of the
        element in the outputs of the apply method of each transition
        (layer) that is used, via fork, as the sequences (input) of the
        next layer. The same element should also appear in the states
        parameter of the apply method.
    fast : bool
        Use the fast, but also memory consuming, implementation of this
        code. By default true.
    skip_connections : bool
        By default False. When true, the sequences of all layers are
        add to the sequences of the apply of this class. When false
        only the sequences of the bottom layer appear in the sequences
        of the apply of this class. In this case the default fork
        used internally between layers has a bias (see fork_prototype.)

    Notes
    -----
    See :class:`.BaseRecurrent` for more initialization parameters.

    """
    @staticmethod
    def suffix(name, level):
        if name == "mask":
            return "mask"
        if level == 0:
            return name
        return name + '_' + str(level)

    @staticmethod
    def suffixes(names, level):
        return [RecurrentStack.suffix(name, level)
                for name in names if name != "mask"]

    @staticmethod
    def split_suffix(name):
        # Target name with suffix to the correct layer
        name_level = name.split('_')
        if len(name_level) == 2:
            name, level = name_level
            level = int(level)
        else:
            # It must be from bottom layer
            level = 0
        return name, level

    def __init__(self, transitions, fork_prototype=None, states_name="states",
                 fast=True, skip_connections=False, **kwargs):
        super(RecurrentStack, self).__init__(**kwargs)

        self.states_name = states_name
        self.skip_connections = skip_connections

        for level, transition in enumerate(transitions):
            transition.name += '_' + str(level)
        self.transitions = transitions

        if fork_prototype is None:
            # If we are not supplied any inputs for the layers above
            # bottom then use bias
            fork_prototype = Linear(use_bias=not skip_connections)
        depth = len(transitions)
        self.forks = [Fork(self.normal_inputs(level),
                           name='fork_' + str(level),
                           prototype=fork_prototype)
                      for level in range(1, depth)]

        self.children = self.transitions + self.forks

        # Programmatically set the apply method
        self.apply = self.fast_apply if fast else self.low_memory_apply
        # parameters of base level are exposed as is
        # excpet for mask which we will put at the ver end. See below.
        for property_ in ["sequences", "states", "outputs"]:
            setattr(self.apply,
                    property_,
                    self.suffixes(getattr(transitions[0].apply, property_), 0)
                )

        # add parameters of other layers
        if skip_connections:
            exposed_parameters = ["sequences", "states", "outputs"]
        else:
            exposed_parameters = ["states", "outputs"]
        for level in range(1, depth):
            for property_ in exposed_parameters:
                setattr(self.apply,
                        property_,
                        getattr(self.apply, property_) +
                        self.suffixes(getattr(transitions[level].apply,
                                              property_),
                                      level)
                        )

        # place mask at end because it has a default value (None)
        # and therefor should come after arguments that may come us
        # unnamed arguments
        if "mask" in transitions[0].apply.sequences:
            self.apply.sequences.append("mask")

        # add context
        self.apply.contexts = list(set(
            sum([transition.apply.contexts for transition in transitions], [])
        ))

        # sum up all the arguments we exepct to see in a call to a transition
        # apply method, anything else is a recursion control
        self.transition_args = set(self.apply.sequences +
                                   self.apply.states +
                                   self.apply.contexts)

    def normal_inputs(self, level):
        return [name for name in self.transitions[level].apply.sequences
                if name != 'mask']

    def _push_allocation_config(self):
        # Configure the forks that connect the "states" element in the `states`
        # of one layer to the elements in the `sequences` of the next layer,
        # excluding "mask".
        # This involves `get_dim` requests
        # to the transitions. To make sure that it answers
        # correctly we should finish its configuration first.
        for transition in self.transitions:
            transition.push_allocation_config()

        for level, fork in enumerate(self.forks):
            fork.input_dim = self.transitions[level].get_dim(self.states_name)
            fork.output_dims = self.transitions[level + 1].get_dims(
                fork.output_names)

    def do_apply(self, *args, **kwargs):
        """Apply the stack of transitions.

        This is the undecorated implementation of the apply method.
        It is separated from the decorated apply method in order to allow
        usage of different docrations (wrappers) to be used.

        Parameters
        ----------
        See docstring of the class for arguments appearing in
        self.apply.sequences, self.apply.states, self.apply.contexts
        All arguments values are of type :class:`~tensor.TensorVariable`.

        In addition the `iterate`, `reverse`, `return_initial_states` or
        any other argument defined in `recurrent_apply` wrapper.

        Returns
        -------
        The outputs of all transitions as defined in `self.apply.outputs`
        All return values are of type :class:`~tensor.TensorVariable`.

        """
        nargs = len(args)
        assert nargs <= len(self.apply.sequences)
        kwargs.update(zip(self.apply.sequences[:nargs], args))

        results = []
        last_states = None
        for level, transition in enumerate(self.transitions):
            normal_inputs = self.normal_inputs(level)
            layer_kwargs = dict()

            if level == 0 or self.skip_connections:
                for name in normal_inputs:
                    layer_kwargs[name] = kwargs.get(self.suffix(name, level))
            if "mask" in transition.apply.sequences:
                layer_kwargs["mask"] = kwargs.get("mask")

            for name in transition.apply.states:
                layer_kwargs[name] = kwargs.get(self.suffix(name, level))

            for name in transition.apply.contexts:
                layer_kwargs[name] = kwargs.get(name)  # contexts has no suffix

            if level > 0:
                # add the forked states of the layer below
                inputs = self.forks[level - 1].apply(last_states, as_list=True)
                for name, input in zip(normal_inputs, inputs):
                    if layer_kwargs.get(name):
                        layer_kwargs[name] += input
                    else:
                        layer_kwargs[name] = input

            # Handle all other arguments
            # For example, if this method is called directly (from fast_apply)
            # then these arguments can be the same arguments that recurrent
            # expects to see such as: 'iterate', 'reverse',
            # 'return_initial_states'
            for k in set(kwargs.keys()) - self.transition_args:
                layer_kwargs[k] = kwargs[k]

            result = transition.apply(as_list=True, **layer_kwargs)
            results.extend(result)

            state_index = transition.apply.outputs.index(self.states_name)
            last_states = result[state_index]
            if kwargs.get('return_initial_states', False):
                # Note that the following line reset the tag
                last_states = last_states[1:]

        return tuple(results)

    @recurrent
    def low_memory_apply(self, *args, **kwargs):
        # we let the recurrent decorator handle the iteration for us
        # so do_apply needs to do a single step.
        kwargs['iterate'] = False
        return self.do_apply(*args, **kwargs)

    @application
    def fast_apply(self, *args, **kwargs):
        # we let the transition in self.transitions each do their iterations
        # separatly, one layer at a time.
        return self.do_apply(*args, **kwargs)

    def get_dim(self, name):
        # Check if we have a contexts element.
        for transition in self.transitions:
            if name in transition.apply.contexts:
                # hopefully there is no conflict between layers about dim
                return transition.get_dim(name)

        name, level = self.split_suffix(name)
        transition = self.transitions[level]
        return transition.get_dim(name)

    @application
    def initial_state(self, state_name, batch_size, *args, **kwargs):
        state_name, level = self.split_suffix(state_name)
        transition = self.transitions[level]
        return transition.initial_state(state_name, batch_size,
                                        *args, **kwargs)


# -----------------------------------------------------------------------------
# Testing

import numpy
import theano
from theano import tensor
import itertools
from numpy.testing import assert_allclose
from blocks.initialization import Constant
from blocks.bricks.recurrent import LSTM
from collections import OrderedDict


class tRecurrentStack(object):
    def setUp(self):
        depth = 4
        self.depth = depth
        dim = 3  # don't change, hardwired in the code
        tarnsitions = [LSTM(dim=dim) for _ in range(depth)]
        self.stack0 = RecurrentStack(tarnsitions,
                                     weights_init=Constant(2),
                                     biases_init=Constant(0))
        self.stack0.initialize()
        self.stack1 = RecurrentStack(tarnsitions,
                                     weights_init=Constant(2),
                                     biases_init=Constant(0),
                                     fast=False)
        self.stack1.initialize()

        self.stack2 = RecurrentStack(tarnsitions,
                                     weights_init=Constant(2),
                                     biases_init=Constant(0),
                                     skip_connections=True)
        self.stack2.initialize()

        self.stack3 = RecurrentStack(tarnsitions,
                                     weights_init=Constant(2),
                                     biases_init=Constant(0),
                                     skip_connections=True,
                                     fast=False)
        self.stack3.initialize()

    def do_one_step(self, stack, skip_connections=False):
        depth = self.depth

        # batch=2
        h0_val = 0.1 * numpy.array([[[1, 1, 0], [0, 1, 1]]] * depth,
                                   dtype=theano.config.floatX)
        c0_val = 0.1 * numpy.array([[[1, 1, 0], [0, 1, 1]]] * depth,
                                   dtype=theano.config.floatX)
        x_val = 0.1 * numpy.array([range(12), range(12, 24)],
                                  dtype=theano.config.floatX)
        # we will use same weights on all layers
        W_state2x_val = 2 * numpy.ones((3, 12), dtype=theano.config.floatX)
        W_state_val = 2 * numpy.ones((3, 12), dtype=theano.config.floatX)
        W_cell_to_in = 2 * numpy.ones((3,), dtype=theano.config.floatX)
        W_cell_to_out = 2 * numpy.ones((3,), dtype=theano.config.floatX)
        W_cell_to_forget = 2 * numpy.ones((3,), dtype=theano.config.floatX)
        W_input2input = 2 * numpy.ones((12, 12), dtype=theano.config.floatX)

        kwargs = OrderedDict()
        for d in range(depth):
            if d > 0:
                suffix = '_' + str(d)
            else:
                suffix = ''
            if d == 0 or skip_connections:
                kwargs['inputs' + suffix] = tensor.matrix('inputs' + suffix)
                kwargs['inputs' + suffix].tag.test_value = x_val
            kwargs['states' + suffix] = tensor.matrix('states' + suffix)
            kwargs['states' + suffix].tag.test_value = h0_val[d]
            kwargs['cells' + suffix] = tensor.matrix('cells' + suffix)
            kwargs['cells' + suffix].tag.test_value = c0_val[d]
        results = stack.apply(iterate=False, **kwargs)
        next_h = theano.function(inputs=list(kwargs.values()),
                                 outputs=results)

        def sigmoid(x):
            return 1. / (1. + numpy.exp(-x))

        h1_val = []
        x_v = x_val
        args_val = []
        for d in range(depth):
            if d == 0 or skip_connections:
                args_val.append(x_val)
            h0_v = h0_val[d]
            args_val.append(h0_v)
            c0_v = c0_val[d]
            args_val.append(c0_v)

            # omitting biases because they are zero
            activation = numpy.dot(h0_v, W_state_val) + x_v
            if skip_connections and d > 0:
                activation += x_val

            i_t = sigmoid(activation[:, :3] + c0_v * W_cell_to_in)
            f_t = sigmoid(activation[:, 3:6] + c0_v * W_cell_to_forget)
            next_cells = f_t * c0_v + i_t * numpy.tanh(activation[:, 6:9])
            o_t = sigmoid(activation[:, 9:12] +
                          next_cells * W_cell_to_out)
            h1_v = o_t * numpy.tanh(next_cells)
            # current layer output state transformed to input of next
            x_v = numpy.dot(h1_v, W_state2x_val)

            h1_val.append(h1_v)

        res = next_h(*args_val)
        for d in range(depth):
            assert_allclose(h1_val[d], res[d * 2], rtol=1e-6)

    def test_one_step(self):
        self.do_one_step(self.stack0)
        self.do_one_step(self.stack1)
        self.do_one_step(self.stack2, skip_connections=True)
        self.do_one_step(self.stack3, skip_connections=True)

    def do_many_steps(self, stack, skip_connections=False):
        depth = self.depth

        # 24 steps
        #  4 batch examples
        # 12 dimensions per step
        x_val = (0.1 * numpy.asarray(
            list(itertools.islice(itertools.permutations(range(12)), 0, 24)),
            dtype=theano.config.floatX))
        x_val = numpy.ones((24, 4, 12),
                           dtype=theano.config.floatX) * x_val[:, None, :]
        # mask the last third of steps
        mask_val = numpy.ones((24, 4), dtype=theano.config.floatX)
        mask_val[12:24, 3] = 0
        # unroll all states and cells for all steps and also initial value
        h_val = numpy.zeros((depth, 25, 4, 3), dtype=theano.config.floatX)
        c_val = numpy.zeros((depth, 25, 4, 3), dtype=theano.config.floatX)
        # we will use same weights on all layers
        W_state2x_val = 2 * numpy.ones((3, 12), dtype=theano.config.floatX)
        W_state_val = 2 * numpy.ones((3, 12), dtype=theano.config.floatX)
        W_cell_to_in = 2 * numpy.ones((3,), dtype=theano.config.floatX)
        W_cell_to_out = 2 * numpy.ones((3,), dtype=theano.config.floatX)
        W_cell_to_forget = 2 * numpy.ones((3,), dtype=theano.config.floatX)
        W_input2input = 2 * numpy.ones((12, 12), dtype=theano.config.floatX)

        kwargs = OrderedDict()

        for d in range(depth):
            if d > 0:
                suffix = '_' + str(d)
            else:
                suffix = ''
            if d == 0 or skip_connections:
                kwargs['inputs' + suffix] = tensor.tensor3('inputs' + suffix)
                kwargs['inputs' + suffix].tag.test_value = x_val

        kwargs['mask'] = tensor.matrix('mask')
        kwargs['mask'].tag.test_value = mask_val
        results = stack.apply(iterate=True, **kwargs)
        calc_h = theano.function(inputs=list(kwargs.values()),
                                 outputs=results)

        def sigmoid(x):
            return 1. / (1. + numpy.exp(-x))

        for i in range(1, 25):
            x_v = x_val[i - 1]
            h_vs = []
            c_vs = []
            for d in range(depth):
                h_v = h_val[d][i - 1, :, :]
                c_v = c_val[d][i - 1, :, :]
                activation = numpy.dot(h_v, W_state_val) + x_v
                if skip_connections and d > 0:
                    activation += x_val[i - 1]

                i_t = sigmoid(activation[:, :3] + c_v * W_cell_to_in)
                f_t = sigmoid(activation[:, 3:6] + c_v * W_cell_to_forget)
                c_v1 = f_t * c_v + i_t * numpy.tanh(activation[:, 6:9])
                o_t = sigmoid(activation[:, 9:12] +
                              c_v1 * W_cell_to_out)
                h_v1 = o_t * numpy.tanh(c_v1)
                h_v = (mask_val[i - 1, :, None] * h_v1 +
                       (1 - mask_val[i - 1, :, None]) * h_v)
                c_v = (mask_val[i - 1, :, None] * c_v1 +
                       (1 - mask_val[i - 1, :, None]) * c_v)
                # current layer output state transformed to input of next
                x_v = numpy.dot(h_v, W_state2x_val)

                h_vs.append(h_v)
                c_vs.append(c_v)

            for d in range(depth):
                h_val[d][i, :, :] = h_vs[d]
                c_val[d][i, :, :] = c_vs[d]

        args_val = [x_val]*(depth if skip_connections else 1) + [mask_val]
        res = calc_h(*args_val)
        for d in range(depth):
            assert_allclose(h_val[d][1:], res[d * 2], rtol=1e-4)
            assert_allclose(c_val[d][1:], res[d * 2 + 1], rtol=1e-4)

    def test_many_steps(self):
        self.do_many_steps(self.stack0)
        self.do_many_steps(self.stack1)
        self.do_many_steps(self.stack2, skip_connections=True)
        self.do_many_steps(self.stack3, skip_connections=True)


if __name__ == "__main__":
    test = tRecurrentStack()
    test.setUp()
    test.test_one_step()
    test.test_many_steps()
