# Copyright 2018 D-Wave Systems Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.
#
# ================================================================================================

from __future__ import division

import itertools

import numpy as np
import dimod

from six import iteritems, itervalues

from dwave.embedding.chain_breaks import majority_vote, broken_chains
from dwave.embedding.exceptions import MissingEdgeError, MissingChainError, InvalidNodeError
from dwave.embedding.utils import chain_to_quadratic


__all__ = ['embed_bqm',
           'embed_ising',
           'embed_qubo',
           'unembed_sampleset',
           ]


def embed_bqm(source_bqm, embedding, target_adjacency, chain_strength=1.0,
              smear_vartype=None):
    """Embed a binary quadratic model onto a target graph.

    Args:
        source_bqm (:obj:`.BinaryQuadraticModel`):
            Binary quadratic model to embed.

        embedding (dict):
            Mapping from source graph to target graph as a dict of form {s: {t, ...}, ...},
            where s is a source-model variable and t is a target-model variable.

        target_adjacency (dict/:class:`networkx.Graph`):
            Adjacency of the target graph as a dict of form {t: Nt, ...},
            where t is a variable in the target graph and Nt is its set of neighbours.

        chain_strength (float, optional):
            Magnitude of the quadratic bias (in SPIN-space) applied between variables to create chains. Note
            that the energy penalty of chain breaks is 2 * `chain_strength`.

        smear_vartype (:class:`.Vartype`, optional, default=None):
            When a single variable is embedded, it's linear bias is 'smeared' evenly over the
            chain. This parameter determines whether the variable is smeared in SPIN or BINARY
            space. By default the embedding is done according to the given source_bqm.

    Returns:
        :obj:`.BinaryQuadraticModel`: Target binary quadratic model.

    Examples:
        This example embeds a fully connected :math:`K_3` graph onto a square target graph.
        Embedding is accomplished by an edge contraction operation on the target graph:
        target-nodes 2 and 3 are chained to represent source-node c.

        >>> import dimod
        >>> import networkx as nx
        >>> # Binary quadratic model for a triangular source graph
        >>> bqm = dimod.BinaryQuadraticModel.from_ising({}, {('a', 'b'): 1, ('b', 'c'): 1, ('a', 'c'): 1})
        >>> # Target graph is a graph
        >>> target = nx.cycle_graph(4)
        >>> # Embedding from source to target graphs
        >>> embedding = {'a': {0}, 'b': {1}, 'c': {2, 3}}
        >>> # Embed the BQM
        >>> target_bqm = dimod.embed_bqm(bqm, embedding, target)
        >>> target_bqm.quadratic[(0, 1)] == bqm.quadratic[('a', 'b')]
        True
        >>> target_bqm.quadratic   # doctest: +SKIP
        {(0, 1): 1.0, (0, 3): 1.0, (1, 2): 1.0, (2, 3): -1.0}

        This example embeds a fully connected :math:`K_3` graph onto the target graph
        of a dimod reference structured sampler, `StructureComposite`, using the dimod reference
        `ExactSolver` sampler with a square graph specified. Target-nodes 2 and 3
        are chained to represent source-node c.

        >>> import dimod
        >>> # Binary quadratic model for a triangular source graph
        >>> bqm = dimod.BinaryQuadraticModel.from_ising({}, {('a', 'b'): 1, ('b', 'c'): 1, ('a', 'c'): 1})
        >>> # Structured dimod sampler with a structure defined by a square graph
        >>> sampler = dimod.StructureComposite(dimod.ExactSolver(), [0, 1, 2, 3], [(0, 1), (1, 2), (2, 3), (0, 3)])
        >>> # Embedding from source to target graph
        >>> embedding = {'a': {0}, 'b': {1}, 'c': {2, 3}}
        >>> # Embed the BQM
        >>> target_bqm = dimod.embed_bqm(bqm, embedding, sampler.adjacency)
        >>> # Sample
        >>> samples = sampler.sample(target_bqm)
        >>> samples.record.sample   # doctest: +SKIP
        array([[-1, -1, -1, -1],
               [ 1, -1, -1, -1],
               [ 1,  1, -1, -1],
               [-1,  1, -1, -1],
               [-1,  1,  1, -1],
        >>> # Snipped above samples for brevity

    """
    if smear_vartype is dimod.SPIN and source_bqm.vartype is dimod.BINARY:
        return embed_bqm(source_bqm.spin, embedding, target_adjacency,
                         chain_strength=chain_strength, smear_vartype=None).binary
    elif smear_vartype is dimod.BINARY and source_bqm.vartype is dimod.SPIN:
        return embed_bqm(source_bqm.binary, embedding, target_adjacency,
                         chain_strength=chain_strength, smear_vartype=None).spin

    # create a new empty binary quadratic model with the same class as source_bqm
    target_bqm = source_bqm.empty(source_bqm.vartype)

    # add the offset
    target_bqm.add_offset(source_bqm.offset)

    # start with the linear biases, spreading the source bias equally over the target variables in
    # the chain
    for v, bias in iteritems(source_bqm.linear):

        if v in embedding:
            chain = embedding[v]
        else:
            raise MissingChainError(v)

        if any(u not in target_adjacency for u in chain):
            raise InvalidNodeError(v, next(u not in target_adjacency for u in chain))

        b = bias / len(chain)

        target_bqm.add_variables_from({u: b for u in chain})

    # next up the quadratic biases, spread the quadratic biases evenly over the available
    # interactions
    for (u, v), bias in iteritems(source_bqm.quadratic):
        available_interactions = {(s, t) for s in embedding[u] for t in embedding[v] if s in target_adjacency[t]}

        if not available_interactions:
            raise MissingEdgeError(u, v)

        b = bias / len(available_interactions)

        target_bqm.add_interactions_from((u, v, b) for u, v in available_interactions)

    for chain in itervalues(embedding):

        # in the case where the chain has length 1, there are no chain quadratic biases, but we
        # none-the-less want the chain variables to appear in the target_bqm
        if len(chain) == 1:
            v, = chain
            target_bqm.add_variable(v, 0.0)
            continue

        quadratic_chain_biases = chain_to_quadratic(chain, target_adjacency, chain_strength)
        target_bqm.add_interactions_from(quadratic_chain_biases, vartype=dimod.SPIN)  # these are spin

        # add the energy for satisfied chains to the offset
        energy_diff = -sum(itervalues(quadratic_chain_biases))
        target_bqm.add_offset(energy_diff)

    return target_bqm


def embed_ising(source_h, source_J, embedding, target_adjacency, chain_strength=1.0):
    """Embed an Ising problem onto a target graph.

    Args:
        source_h (dict[variable, bias]/list[bias]):
            Linear biases of the Ising problem. If a list, the list's indices are used as
            variable labels.

        source_J (dict[(variable, variable), bias]):
            Quadratic biases of the Ising problem.

        embedding (dict):
            Mapping from source graph to target graph as a dict of form {s: {t, ...}, ...},
            where s is a source-model variable and t is a target-model variable.

        target_adjacency (dict/:class:`networkx.Graph`):
            Adjacency of the target graph as a dict of form {t: Nt, ...},
            where t is a target-graph variable and Nt is its set of neighbours.

        chain_strength (float, optional):
            Magnitude of the quadratic bias (in SPIN-space) applied between variables to form a chain. Note
            that the energy penalty of chain breaks is 2 * `chain_strength`.

    Returns:
        tuple: A 2-tuple:

            dict[variable, bias]: Linear biases of the target Ising problem.

            dict[(variable, variable), bias]: Quadratic biases of the target Ising problem.

    Examples:
        This example embeds a fully connected :math:`K_3` graph onto a square target graph.
        Embedding is accomplished by an edge contraction operation on the target graph: target-nodes
        2 and 3 are chained to represent source-node c.

        >>> import dimod
        >>> import networkx as nx
        >>> # Ising problem for a triangular source graph
        >>> h = {}
        >>> J = {('a', 'b'): 1, ('b', 'c'): 1, ('a', 'c'): 1}
        >>> # Target graph is a square graph
        >>> target = nx.cycle_graph(4)
        >>> # Embedding from source to target graph
        >>> embedding = {'a': {0}, 'b': {1}, 'c': {2, 3}}
        >>> # Embed the Ising problem
        >>> target_h, target_J = dimod.embed_ising(h, J, embedding, target)
        >>> target_J[(0, 1)] == J[('a', 'b')]
        True
        >>> target_J        # doctest: +SKIP
        {(0, 1): 1.0, (0, 3): 1.0, (1, 2): 1.0, (2, 3): -1.0}

        This example embeds a fully connected :math:`K_3` graph onto the target graph
        of a dimod reference structured sampler, `StructureComposite`, using the dimod reference
        `ExactSolver` sampler with a square graph specified. Target-nodes 2 and 3 are chained to
        represent source-node c.

        >>> import dimod
        >>> # Ising problem for a triangular source graph
        >>> h = {}
        >>> J = {('a', 'b'): 1, ('b', 'c'): 1, ('a', 'c'): 1}
        >>> # Structured dimod sampler with a structure defined by a square graph
        >>> sampler = dimod.StructureComposite(dimod.ExactSolver(), [0, 1, 2, 3], [(0, 1), (1, 2), (2, 3), (0, 3)])
        >>> # Embedding from source to target graph
        >>> embedding = {'a': {0}, 'b': {1}, 'c': {2, 3}}
        >>> # Embed the Ising problem
        >>> target_h, target_J = dimod.embed_ising(h, J, embedding, sampler.adjacency)
        >>> # Sample
        >>> samples = sampler.sample_ising(target_h, target_J)
        >>> for sample in samples.samples(n=3, sorted_by='energy'):   # doctest: +SKIP
        ...     print(sample)
        ...
        {0: 1, 1: -1, 2: -1, 3: -1}
        {0: 1, 1: 1, 2: -1, 3: -1}
        {0: -1, 1: 1, 2: -1, 3: -1}

    """
    source_bqm = dimod.BinaryQuadraticModel.from_ising(source_h, source_J)
    target_bqm = embed_bqm(source_bqm, embedding, target_adjacency, chain_strength=chain_strength)
    target_h, target_J, __ = target_bqm.to_ising()
    return target_h, target_J


def embed_qubo(source_Q, embedding, target_adjacency, chain_strength=1.0):
    """Embed a QUBO onto a target graph.

    Args:
        source_Q (dict[(variable, variable), bias]):
            Coefficients of a quadratic unconstrained binary optimization (QUBO) model.

        embedding (dict):
            Mapping from source graph to target graph as a dict of form {s: {t, ...}, ...},
            where s is a source-model variable and t is a target-model variable.

        target_adjacency (dict/:class:`networkx.Graph`):
            Adjacency of the target graph as a dict of form {t: Nt, ...},
            where t is a target-graph variable and Nt is its set of neighbours.

        chain_strength (float, optional):
            Magnitude of the quadratic bias (in SPIN-space) applied between variables to form a chain. Note
            that the energy penalty of chain breaks is 2 * `chain_strength`.

    Returns:
        dict[(variable, variable), bias]: Quadratic biases of the target QUBO.

    Examples:
        This example embeds a square source graph onto fully connected :math:`K_5` graph.
        Embedding is accomplished by an edge deletion operation on the target graph: target-node
        0 is not used.

        >>> import dimod
        >>> import networkx as nx
        >>> # QUBO problem for a square graph
        >>> Q = {(1, 1): -4.0, (1, 2): 4.0, (2, 2): -4.0, (2, 3): 4.0,
        ...      (3, 3): -4.0, (3, 4): 4.0, (4, 1): 4.0, (4, 4): -4.0}
        >>> # Target graph is a fully connected k5 graph
        >>> K_5 = nx.complete_graph(5)
        >>> 0 in K_5
        True
        >>> # Embedding from source to target graph
        >>> embedding = {1: {4}, 2: {3}, 3: {1}, 4: {2}}
        >>> # Embed the QUBO
        >>> target_Q = dimod.embed_qubo(Q, embedding, K_5)
        >>> (0, 0) in target_Q
        False
        >>> target_Q     # doctest: +SKIP
        {(1, 1): -4.0,
         (1, 2): 4.0,
         (2, 2): -4.0,
         (2, 4): 4.0,
         (3, 1): 4.0,
         (3, 3): -4.0,
         (4, 3): 4.0,
         (4, 4): -4.0}

        This example embeds a square graph onto the target graph of a dimod reference structured
        sampler, `StructureComposite`, using the dimod reference `ExactSolver` sampler with a
        fully connected :math:`K_5` graph specified.

        >>> import dimod
        >>> import networkx as nx
        >>> # QUBO problem for a square graph
        >>> Q = {(1, 1): -4.0, (1, 2): 4.0, (2, 2): -4.0, (2, 3): 4.0,
        ...      (3, 3): -4.0, (3, 4): 4.0, (4, 1): 4.0, (4, 4): -4.0}
        >>> # Structured dimod sampler with a structure defined by a K5 graph
        >>> sampler = dimod.StructureComposite(dimod.ExactSolver(), list(K_5.nodes), list(K_5.edges))
        >>> sampler.adjacency      # doctest: +SKIP
        {0: {1, 2, 3, 4},
         1: {0, 2, 3, 4},
         2: {0, 1, 3, 4},
         3: {0, 1, 2, 4},
         4: {0, 1, 2, 3}}
        >>> # Embedding from source to target graph
        >>> embedding = {0: [4], 1: [3], 2: [1], 3: [2], 4: [0]}
        >>> # Embed the QUBO
        >>> target_Q = dimod.embed_qubo(Q, embedding, sampler.adjacency)
        >>> # Sample
        >>> samples = sampler.sample_qubo(target_Q)
        >>> for datum in samples.data():   # doctest: +SKIP
        ...     print(datum)
        ...
        Sample(sample={1: 0, 2: 1, 3: 1, 4: 0}, energy=-8.0)
        Sample(sample={1: 1, 2: 0, 3: 0, 4: 1}, energy=-8.0)
        Sample(sample={1: 1, 2: 0, 3: 0, 4: 0}, energy=-4.0)
        Sample(sample={1: 1, 2: 1, 3: 0, 4: 0}, energy=-4.0)
        Sample(sample={1: 0, 2: 1, 3: 0, 4: 0}, energy=-4.0)
        Sample(sample={1: 1, 2: 1, 3: 1, 4: 0}, energy=-4.0)
        >>> # Snipped above samples for brevity

    """
    source_bqm = dimod.BinaryQuadraticModel.from_qubo(source_Q)
    target_bqm = embed_bqm(source_bqm, embedding, target_adjacency, chain_strength=chain_strength)
    target_Q, __ = target_bqm.to_qubo()
    return target_Q


def unembed_sampleset(target_sampleset, embedding, source_bqm,
                      chain_break_method=None, chain_break_fraction=False):
    """Unembed the samples set.

    Construct a sample set for the source binary quadratic model (BQM) by
    unembedding the given samples from the target BQM.

    Args:
        target_sampleset (:obj:`dimod.SampleSet`):
            SampleSet from the target BQM.

        embedding (dict):
            Mapping from source graph to target graph as a dict of form
            {s: {t, ...}, ...}, where s is a source variable and t is a target
            variable.

        source_bqm (:obj:`dimod.BinaryQuadraticModel`):
            Source binary quadratic model.

        chain_break_method (function, optional):
            Method used to resolve chain breaks.
            See :mod:`dwave.embedding.chain_breaks`.

        chain_break_fraction (bool, optional, default=False):
            If True, a 'chain_break_fraction' field is added to the unembedded
            samples which report what fraction of the chains were broken before
            unembedding.

    Returns:
        :obj:`.SampleSet`:

    Examples:

        >>> import dimod
        ...
        >>> # say we have a bqm on a triangle and an embedding
        >>> J = {('a', 'b'): -1, ('b', 'c'): -1, ('a', 'c'): -1}
        >>> bqm = dimod.BinaryQuadraticModel.from_ising({}, J)
        >>> embedding = {'a': [0, 1], 'b': [2], 'c': [3]}
        ...
        >>> # and some samples from the embedding
        >>> samples = [{0: -1, 1: -1, 2: -1, 3: -1},  # [0, 1] is unbroken
                       {0: -1, 1: +1, 2: +1, 3: +1}]  # [0, 1] is broken
        >>> energies = [-3, 1]
        >>> embedded = dimod.SampleSet.from_samples(samples, dimod.SPIN, energies)
        ...
        >>> # unembed
        >>> samples = dwave.embedding.unembed_sampleset(embedded, embedding, bqm)
        >>> samples.record.sample   # doctest: +SKIP
        array([[-1, -1, -1],
               [ 1,  1,  1]], dtype=int8)

    """

    if chain_break_method is None:
        chain_break_method = majority_vote

    variables = list(source_bqm)
    try:
        chains = [embedding[v] for v in variables]
    except KeyError:
        raise ValueError("given bqm does not match the embedding")

    chain_idxs = [[target_sampleset.variables.index[v] for v in chain] for chain in chains]

    record = target_sampleset.record

    unembedded, idxs = chain_break_method(record.sample, chain_idxs)

    # dev note: this is a bug in dimod that empty unembedded is not handled,
    # in the future this try-except can be removed
    try:
        energies = source_bqm.energies((unembedded, variables))
    except ValueError:
        datatypes = [('sample', np.dtype(np.int8), (len(variables),)), ('energy', np.float)]
        datatypes.extend((name, record[name].dtype, record[name].shape[1:])
                         for name in record.dtype.names
                         if name not in {'sample', 'energy'})
        if chain_break_fraction:
            datatypes.append(('chain_break_fraction', np.float64))
        # there are no samples so everything is empty
        data = np.rec.array(np.empty(0, dtype=datatypes))
        return dimod.SampleSet(data, variables, target_sampleset.info.copy(), target_sampleset.vartype)

    reserved = {'sample', 'energy'}
    vectors = {name: record[name][idxs]
               for name in record.dtype.names if name not in reserved}

    if chain_break_fraction:
        vectors['chain_break_fraction'] = broken_chains(record.sample, chain_idxs).mean(axis=1)[idxs]

    return dimod.SampleSet.from_samples((unembedded, variables),
                                        target_sampleset.vartype,
                                        energy=energies,
                                        info=target_sampleset.info.copy(),
                                        **vectors)
