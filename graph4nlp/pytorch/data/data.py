"""
The Graph4NLP library uses the class :py:class:`GraphData` as the representation for structured data (graphs).
:py:class:`GraphData` supports basic operations to the graph, including adding nodes and edges. :py:class:`GraphData` also
supports adding features which are in tensor form, and attributes which are of arbitrary form to the corresponding
nodes or edges. Batching operations is also supported by :py:class:`GraphData`.

"""
import warnings
from collections import namedtuple

import dgl
import numpy as np
import scipy.sparse
import torch
from torch.nn.utils.rnn import pad_sequence, pack_padded_sequence

from .utils import SizeMismatchException, EdgeNotFoundException
from .utils import check_and_expand, int_to_list, entail_zero_padding, slice_to_list
from .views import NodeView, NodeFeatView, EdgeView, BatchNodeFeatView, BatchEdgeFeatView

EdgeIndex = namedtuple('EdgeIndex', ['src', 'tgt'])

node_feature_factory = dict
node_attribute_factory = list
single_node_attr_factory = dict
res_init_node_attr = {'node_attr': None}
res_init_node_features = {'node_feat': None, 'node_emb': None}

eid_nids_mapping_factory = dict
nids_eid_mapping_factory = dict
edge_feature_factory = dict
edge_attribute_factory = list
single_edge_attr_factory = dict
res_init_edge_features = {'edge_feat': None, 'edge_emb': None, 'edge_weight': None}
res_init_edge_attributes = {'edge_attr': None}

graph_data_factory = dict


class GraphData(object):
    """
    Represent a single graph with additional attributes.
    """

    def __init__(self, src=None, device=None):
        """
        Parameters
        ----------
        src: GraphData, default=None
            The source graph. If not None, then the newly generated graph is a copy of :py:class:`src`.
        device: str, default=None
            The device descriptor for graph. By default it is None.
        """

        # Initialize internal data storages.
        self._node_attributes = node_attribute_factory()
        self._node_features = node_feature_factory(res_init_node_features)
        self._edge_indices = EdgeIndex(src=[], tgt=[])
        self._nids_eid_mapping = nids_eid_mapping_factory()
        self._edge_features = edge_feature_factory(res_init_edge_features)
        self._edge_attributes = edge_attribute_factory()
        self.graph_attributes = graph_data_factory()
        self.device = device

        # Batch information. If this instance is not a batch, then the following attributes are all `None`.
        self._is_batch = False  # Bool flag indicating whether this graph is a batch graph
        self.batch = None  # Batch node indices
        self.batch_size = None  # Batch size
        self._batch_num_nodes = None  # Subgraph node number list with the length of batch size
        self._batch_num_edges = None  # Subgraph edge number list with the length of batch size

        if src is not None:
            if isinstance(src, GraphData):
                self.from_graphdata(src)
            else:
                raise NotImplementedError

    def to(self, device):
        self.device = device
        for k, v in self._node_features.items():
            if isinstance(v, torch.Tensor):
                self._node_features[k] = v.to(device)
        for k, v in self._edge_features.items():
            if isinstance(v, torch.Tensor):
                self._edge_features[k] = v.to(device)
        return self

    # Node operations
    @property
    def nodes(self) -> NodeView:
        """
        Return a node view through which the user can access the features and attributes

        Returns
        -------
        node: NodeView
            The node view
        """
        return NodeView(self)

    def get_node_num(self) -> int:
        """
        Get the number of nodes in the graph.

        Returns
        -------
        num_nodes: int
            The number of nodes in the graph.
        """
        return len(self._node_attributes)

    def add_nodes(self, node_num: int) -> None:
        """
        Add a number of nodes to the graph.

        Parameters
        ------
        node_num: int
            The number of nodes to be added
        """
        assert node_num > 0, "The number of nodes to be added should be greater than 0. (Got {})".format(node_num)

        # Create placeholders in the node attribute dictionary
        self._node_attributes.extend([single_node_attr_factory(**res_init_node_attr) for _ in range(node_num)])

        # Do padding in the node feature dictionary
        for key in self._node_features.keys():
            self._node_features[key] = entail_zero_padding(self._node_features[key], node_num)

    # Node feature operations
    @property
    def node_features(self) -> NodeFeatView:
        """
        Access and modify node feature vectors (tensor).
        This property can be accessed in a dict-of-dict fashion, with the order being [name][index].
        'name' indicates the name of the feature vector. 'index' selects the specific nodes to be accessed.
        When accessed independently, returns the feature dictionary with the format {name: tensor}

        Examples
        --------
        >>> g = GraphData()
        >>> g.add_nodes(10)
        >>> import torch
        >>> g.node_features['x'] = torch.rand((10, 10))
        >>> g.node_features['x'][0]
        torch.Tensor([0.1036, 0.6757, 0.4702, 0.8938, 0.6337, 0.3290, 0.6739, 0.1091, 0.7996, 0.0586])

        Returns
        -------
        NodeFeatView
        """

        return self.nodes[:].features

    def get_node_features(self, nodes: int or slice) -> torch.Tensor:
        """
        Get the node feature dictionary of the `nodes`

        Parameters
        ----------
        nodes: int or slice
            The nodes to be accessed

        Returns
        -------
        node_features: dict
            The reference dict of the actual tensor
        """
        ret = dict()
        for key in self._node_features.keys():
            if self._node_features[key] is None:
                ret[key] = None
            else:
                ret[key] = self._node_features[key][nodes]
        return ret

    def node_feature_names(self):
        return self._node_features.keys()

    def set_node_features(self, nodes: int or slice, new_data: dict) -> None:
        """
        Set the features of the `nodes` with the given `new_data``.

        Parameters
        ----------
        nodes: int or slice
            The nodes involved
        new_data: dict
            The new data to write. Key indicates feature name and value indicates the actual value

        Raises
        ----------
        SizeMismatchException
            If the size of the new features does not match the node number
        """
        # Consistency check
        for key in new_data.keys():
            if key not in self._node_features or self._node_features[key] is None:  # A new feature is added
                # If the shape of the new feature does not match the number of existing nodes, then error occurs
                if (not isinstance(nodes, slice)) or (
                        len(slice_to_list(nodes, self.get_node_num())) != self.get_node_num()):
                    raise ValueError(
                        'The new feature `{}\' should cover all existing {} nodes!'.format(key, self.get_node_num()))

        # Modification
        for key, value in new_data.items():
            # Node-shape check
            assert value.shape[0] == self.get_node_num(), \
                "The shape feature '{}' does not match the number of nodes in the graph. Got a {} tensor but have {} nodes.".format(
                    key, value.shape, self.get_node_num())

            assert isinstance(value, torch.Tensor), "`{}' is not a tensor. Node features are expected to be tensor."

            value_on_device = value
            if key not in self._node_features or self._node_features[key] is None:
                self._node_features[key] = value_on_device
            else:
                if nodes == slice(None, None, None):
                    self._node_features[key] = value_on_device
                else:
                    self._node_features[key][nodes] = value_on_device

    # Node attribute operations
    @property
    def node_attributes(self) -> list:
        """
        Access node attribute dictionary

        Returns
        -------
        node_attribute_dict: dict
            The dict of node attributes
        """
        return self._node_attributes

    def get_node_attrs(self, nodes: int or slice):
        """
        Get the attributes of the given `nodes`.

        Parameters
        ----------
        nodes: int
         or slice
            The given node index

        Returns
        -------
        list
            The node attribute dictionary.
        """
        # if isinstance(nodes, slice):
        #     node_idx = slice_to_list(nodes, self.get_node_num())
        # else:
        #     node_idx = [nodes]
        #
        # ret = {}
        # for idx in node_idx:
        #     ret[idx] = self._node_attributes[idx]
        # return ret
        return self._node_attributes[nodes]

    # Edge views and operations
    @property
    def edges(self):
        """
        Return an edge view of the edges and the corresponding data

        Returns
        -------
        edges: EdgeView
        """
        return EdgeView(self)

    def get_edge_num(self) -> int:
        """
        Get the number of edges in the graph

        Returns
        -------
        num_edges: int
            The number of edges
        """
        return len(self._edge_indices.src)

    def add_edge(self, src: int, tgt: int):
        """
        Add one edge to the graph.

        Parameters
        ----------
        src: int
            Source node index
        tgt: int
            Target node index

        Raises
        ------
        ValueError
            If one of the endpoints of the edge doesn't exist in the graph.
        """
        # Consistency check
        if (src < 0 or src >= self.get_node_num()) and (tgt < 0 and tgt >= self.get_node_num()):
            raise ValueError('Endpoint not in the graph.')

        # Duplicate edge check. If the edge to be added already exists in the graph, then skip it.
        endpoint_tuple = (src, tgt)
        if endpoint_tuple in self._nids_eid_mapping.keys():
            warnings.warn('Edge {} is already in the graph. Skipping this edge.'.format(endpoint_tuple), Warning)
            return

        # Append to the mapping list
        eid = self.get_edge_num()
        self._nids_eid_mapping[endpoint_tuple] = eid

        # Add edge
        self._edge_indices.src.append(src)
        self._edge_indices.tgt.append(tgt)

        # Initialize edge feature and attribute
        # 1. create placeholder in edge attribute dictionary
        self._edge_attributes.append(single_edge_attr_factory(**res_init_edge_attributes))
        # 2. perform zero padding
        for key in self._edge_features.keys():
            self._edge_features[key] = entail_zero_padding(self._edge_features[key], 1)

    def add_edges(self, src: int or list, tgt: int or list):
        """
        Add a bunch of edges to the graph.

        Parameters
        ----------
        src: int or list
            Source node indices
        tgt: int or list
            Target node indices

        Raises
        ------
        ValueError
            If the lengths of `src` and `tgt` don't match or one of the list contains no element.
        """
        src, tgt = check_and_expand(int_to_list(src), int_to_list(tgt))
        assert len(src) == len(tgt), "Length of the source and target indices is not the same. " \
                                     "Got {} source nodes and {} target nodes".format(len(src), len(tgt))
        for src_idx, tgt_idx in zip(src, tgt):
            # Consistency check
            if (src_idx < 0 or src_idx >= self.get_node_num()) and (tgt_idx < 0 and tgt_idx >= self.get_node_num()):
                raise ValueError('Endpoint not in the graph.')

        current_num_edges = len(self._edge_attributes)
        duplicate_edge_indices = list()
        for i in range(len(src)):
            # Duplicate edge check. If the edge to be added already exists in the graph, then skip it.
            endpoint_tuple = (src[i], tgt[i])
            if endpoint_tuple in self._nids_eid_mapping.keys():
                warnings.warn('Edge {} is already in the graph. Skipping this edge.'.format(endpoint_tuple), Warning)
                duplicate_edge_indices.append(i)
                continue
            self._nids_eid_mapping[endpoint_tuple] = current_num_edges + i

        # Remove duplicate edges
        duplicate_edge_indices.reverse()  # Needs to be reversed first to avoid index overflow after popping.
        for edge_index in duplicate_edge_indices:
            src.pop(edge_index)
            tgt.pop(edge_index)

        num_edges = len(src)

        # Add edge indices
        self._edge_indices.src.extend(src)
        self._edge_indices.tgt.extend(tgt)

        # Initialize edge attributes and features
        self._edge_attributes.extend([single_edge_attr_factory(**res_init_edge_attributes) for _ in range(num_edges)])
        for key in self._edge_features.keys():
            self._edge_features[key] = entail_zero_padding(self._edge_features[key], num_edges)

    def edge_ids(self, src: int or list, tgt: int or list) -> list:
        """
        Convert the given endpoints to edge indices.

        Parameters
        ----------
        src: int or list
            The index of source node(s).
        tgt: int or list
            The index of target node(s).

        Returns
        -------
        list
            The index of corresponding edges.

        Raises
        ------
        EdgeNotFoundException
            If the edge is not in the graph.
        """
        assert isinstance(src, int) or isinstance(src, list), "`src` should be either int or list."
        assert isinstance(tgt, int) or isinstance(tgt, list), "`tgt` should be either int or list."
        src, tgt = check_and_expand(int_to_list(src), int_to_list(tgt))
        eid_list = []
        try:
            for src_idx, tgt_idx in zip(src, tgt):
                eid_list.append(self._nids_eid_mapping[(src_idx, tgt_idx)])
        except KeyError:
            raise EdgeNotFoundException('Edge {} does not exist!'.format((src, tgt)))
        return eid_list

    def get_all_edges(self) -> list:
        """
        Get all the edges in the graph

        Returns
        -------
        edges: list
            List of edges. Each edge is in the shape of the endpoint tuple (src, dst).
        """
        edges = []
        for i in range(self.get_edge_num()):
            edges.append((self._edge_indices.src[i], self._edge_indices.tgt[i]))
        return edges

    # Edge feature operations
    @property
    def edge_features(self):
        return self.edges[:].features

    def get_edge_feature(self, edges: list):
        """
        Get the feature of the given edges.

        Parameters
        ----------
        edges: list
            Edge indices

        Returns
        -------
        dict
            The dictionary containing all relevant features.
        """
        ret = {}
        for key in self._edge_features.keys():
            if self._edge_features[key] is None:
                ret[key] = None
            else:
                ret[key] = self._edge_features[key][edges]
        return ret

    def get_edge_feature_names(self):
        """Get all the names of edge features"""
        return self._edge_features.keys()

    def set_edge_feature(self, edges: int or slice or list, new_data: dict):
        """
        Set edge feature

        Parameters
        ----------
        edges: int or list or slice
            Edge indices
        new_data: dict
            New data

        Raises
        ----------
        SizeMismatchException
            If the size of the new features does not match the node number
        """
        # Consistency check
        for key in new_data.keys():
            if key not in self._edge_features or self._edge_features[key] is None:  # A new feature is added
                # If the shape of the new feature does not match the number of existing nodes, then error occurs
                if (not isinstance(edges, slice)) or (
                        len(slice_to_list(edges, self.get_edge_num())) != self.get_edge_num()):
                    raise SizeMismatchException(
                        'The new feature `{}\' should cover all existing {} edges!'.format(key, self.get_edge_num()))

        # Modification
        for key, value in new_data.items():
            assert isinstance(value, torch.Tensor), "`{}' is not a tensor. Node features are expected to be tensor."
            assert value.shape[0] == self.get_edge_num(), "Length of the feature vector does not match the edge number." \
                                                          "Got tensor '{}' of shape {} but the graph has only {} edges.".format(
                key, value.shape, self.get_edge_num())
            # Move the new value to the device consistent with current graph
            value_on_device = value.to(self.device)

            if key not in self._edge_features or self._edge_features[key] is None:
                self._edge_features[key] = value_on_device
            elif edges == slice(None, None, None):
                # Same as node features, if the edges to be modified is all the edges in the graph.
                self._edge_features[key] = value_on_device
            else:
                self._edge_features[key][edges] = value_on_device

    # Edge attribute operations
    @property
    def edge_attributes(self):
        return self._edge_attributes

    # Conversion utility functions
    def to_dgl(self) -> dgl.DGLGraph:
        """
        Convert to dgl.DGLGraph

        Returns
        -------
        g: dgl.DGLGraph
            The converted dgl.DGLGraph
        """
        dgl_g = dgl.DGLGraph().to(self.device)
        # Add nodes and their features
        dgl_g.add_nodes(num=self.get_node_num())
        for key, value in self._node_features.items():
            if value is not None:
                dgl_g.ndata[key] = value
        # Add edges and their features
        dgl_g.add_edges(u=self._edge_indices.src, v=self._edge_indices.tgt)
        for key, value in self._edge_features.items():
            if value is not None:
                dgl_g.edata[key] = value
        return dgl_g

    def from_dgl(self, dgl_g: dgl.DGLGraph):
        """
        Build the graph from dgl.DGLGraph

        Parameters
        ----------
        dgl_g: dgl.DGLGraph
            The source graph
        """
        assert self.get_edge_num() == 0 and self.get_node_num() == 0, \
            'This graph isn\'t an empty graph. Please use an empty graph for conversion.'

        # Add nodes
        self.add_nodes(dgl_g.number_of_nodes())
        for k, v in dgl_g.ndata.items():
            self.node_features[k] = v

        # Add edges
        src_tensor, tgt_tensor = dgl_g.edges()
        src_list = list(src_tensor.detach().cpu().numpy())
        tgt_list = list(tgt_tensor.detach().cpu().numpy())
        self.add_edges(src_list, tgt_list)
        for k, v in dgl_g.edata.items():
            self.edge_features[k] = v

    def from_dense_adj(self, adj: torch.Tensor):
        assert adj.dim() == 2, 'Adjancency matrix is not 2-dimensional.'
        assert adj.shape[0] == adj.shape[1], 'Adjancecy is not a square.'

        node_num = adj.shape[0]
        self.add_nodes(node_num)
        edge_weight = []
        for i in range(adj.shape[0]):
            for j in range(adj.shape[1]):
                if adj[i][j] != 0:
                    self.add_edge(i, j)
                    edge_weight.append(adj[i][j])
        edge_weight = torch.stack(edge_weight, dim=0)
        self.edge_features['edge_weight'] = edge_weight

    def from_scipy_sparse_matrix(self, adj: scipy.sparse.coo_matrix):
        assert adj.shape[0] == adj.shape[1], 'Got an adjancecy matrix which is not a square.'

        num_nodes = adj.shape[0]
        self.add_nodes(num_nodes)

        for i in range(adj.row.shape[0]):
            self.add_edge(adj.row[i], adj.col[i])
        self.edge_features['edge_weight'] = torch.tensor(adj.data)

    def adj_matrix(self):
        ret = torch.zeros((self.get_node_num(), self.get_node_num()))
        all_edges = self.edges()
        for i in range(len(all_edges)):
            u, v = all_edges[i]
            ret[u][v] = 1
        return ret

    def scipy_sparse_adj(self):
        row = np.array(self._edge_indices[0])
        col = np.array(self._edge_indices[1])
        data = np.ones(self.get_edge_num())
        matrix = scipy.sparse.coo_matrix((data, (row, col)), shape=(self.get_node_num(), self.get_node_num()))
        return matrix

    def from_graphdata(self, src):
        """Build a clone from a source GraphData"""

        # Add nodes and edges
        self.add_nodes(src.get_node_num())
        self.add_edges(src._edge_indices.src, src._edge_indices.tgt)

        # Deepcopy of feature tensors
        for k, v in src._node_features.items():
            self._node_features[k] = v
        for k, v in src._edge_features.items():
            self._edge_features[k] = v

        # Copy attributes
        import copy
        self._node_attributes = copy.deepcopy(src.node_attributes)
        self._edge_attributes = copy.deepcopy(src.edge_attributes)
        self.graph_attributes = copy.deepcopy(src.graph_attributes)

        # Copy batch information if necessary
        if src._is_batch:
            self.copy_batch_info(src)

        # Move data to the device of the source graph
        self.to(src.device)

    def copy_batch_info(self, batch):
        self._is_batch = True
        self.batch = batch.batch
        self.device = batch.device
        self.batch_size = batch.batch_size
        self._batch_num_edges = batch._batch_num_edges
        self._batch_num_nodes = batch._batch_num_nodes

    @property
    def batch_node_features(self):
        """
        Get a view of the batched(padded) version of the node features. Shape: (B, N, D)

        Returns
        -------
        BatchNodeFeatView
        """
        return BatchNodeFeatView(self)

    def _get_batch_node_features(self, item=None):
        """
        Get the batched view of node feature tensors, i.e., tensors in (B, N, D) view

        Parameters
        -------
        item: str
            The name of the features. If None then return a dictionary of all the features.

        Returns
        -------
        dict or tensor:
            A dictionary containing the node feature names and the corresponding batch-view tensors, or just the
            specified tensor.
        """
        if not self._is_batch:
            raise Exception("Calling batch_node_features() method on a non-batch graph.")
        if item is None:
            batch_node_features = dict()
            separate_features = self.split_node_features
            for k, v in separate_features.items():
                batch_node_features[k] = pad_sequence(list(v), batch_first=True)
            return batch_node_features
        else:
            if (item not in self.node_features.keys()) or (self.node_features[item] is None):
                raise Exception("Node feature {} doesn't exist!".format(item))
            return pad_sequence(self.split_node_features[item], batch_first=True)

    def _set_batch_node_features(self, key, value):
        """
        Set node features in batch view.

        Parameters
        ----------
        key: str
            The name of the feature.
        value: Tensor
            The values to be written, in the shape of (B, N, D)
        """
        individual_features = [value[i, :self._batch_num_nodes[i]] for i in range(len(self._batch_num_nodes))]
        self.set_node_features(slice(None, None, None), {key: torch.cat(individual_features)})

    @property
    def batch_edge_features(self):
        """
        Edge version of self.batch_node_features

        Returns
        -------
        BatchEdgeFeatView
        """
        return BatchEdgeFeatView(self)

    def _get_batch_edge_features(self, item=None):
        """
        An edge version of :py:method `batch_node_features`.

        Returns
        -------
        dict or tensor:
            A dictionary containing the edge feature names and the corresponding batch-view tensors.
        """
        if not self._is_batch:
            raise Exception("Calling batch_edge_features() method on a non-batch graph.")
        if item is None:
            batch_edge_features = dict()
            for k, v in self.split_edge_features.items():
                batch_edge_features[k] = pad_sequence(list(v), batch_first=True)
            return batch_edge_features
        else:
            if (item not in self.edge_features.keys()) or (self.edge_features[item] is None):
                raise Exception("Edge feature {} doesn't exist!".format(item))
            return pad_sequence(self.split_edge_features[item], batch_first=True)

    def _set_batch_edge_features(self, key, value):
        individual_features = [value[i, :self._batch_num_edges[i]] for i in range(len(self._batch_num_edges))]
        self.set_edge_feature(slice(None, None, None), {key: torch.cat(individual_features)})

    @property
    def split_node_features(self):
        if not self._is_batch:
            raise Exception("Calling split_node_features() method on a non-batch graph.")
        node_features = dict()
        for feature in self.node_features.keys():
            if self.node_features[feature] is None:
                continue
            node_features[feature] = torch.split(self.node_features[feature],
                                                 split_size_or_sections=self._batch_num_nodes)
        return node_features

    @property
    def split_edge_features(self):
        if not self._is_batch:
            raise Exception("Calling split_edge_features() method on a non-batch graph.")
        edge_features = dict()
        for feature in self.edge_features.keys():
            if self.edge_features[feature] is None:
                continue
            edge_features[feature] = torch.split(self.edge_features[feature],
                                                 split_size_or_sections=self._batch_num_edges)
        return edge_features


def from_dgl(g: dgl.DGLGraph) -> GraphData:
    """
    Convert a dgl.DGLGraph to a GraphData object.

    Parameters
    ----------
    g: dgl.DGLGraph
        The source graph in DGLGraph format.

    Returns
    -------
    GraphData
        The converted graph in GraphData format.
    """
    graph = GraphData()
    graph.from_dgl(g)
    return graph


def to_batch(graphs: list = None) -> GraphData:
    """
    Convert a list of GraphData to a large graph (a batch).

    Parameters
    ----------
    graphs: list of GraphData
        The list of GraphData to be batched

    Returns
    -------
    GraphData
        The large graph containing all the graphs in the batch.
    """

    # Check
    assert isinstance(graphs, list), "to_batch() only accepts list of GraphData!"
    assert len(graphs) > 0, "Cannot convert an empty list of graphs into a big batched graph!"

    # Optimized version
    big_graph = GraphData()
    big_graph._is_batch = True
    big_graph.device = graphs[0].device

    total_num_nodes = sum([g.get_node_num() for g in graphs])

    # Step 1: Add nodes
    big_graph.add_nodes(total_num_nodes)

    # Step 2: Set node features
    node_features = dict()
    for g in graphs:
        for feature_name in g.node_features.keys():
            if feature_name in node_features:
                node_features[feature_name].append(g.node_features[feature_name])
            else:
                node_features[feature_name] = [g.node_features[feature_name]]
    for k, v in node_features.items():
        if None in v:
            continue
        else:
            feature_tensor = torch.cat(v, dim=0)
            big_graph.node_features[k] = feature_tensor

    # Step 3: Set node attributes
    total_node_count = 0
    for g in graphs:
        for i in range(g.get_node_num()):
            big_graph.node_attributes[total_node_count] = g.node_attributes[i]
            total_node_count += 1

    # Step 4: Add edges
    def stack_edge_indices(gs):
        all_edge_indices = EdgeIndex(src=[], tgt=[])
        cumulative_node_num = 0
        for g in gs:
            for edge_index_tuple in g.get_all_edges():
                src, tgt = edge_index_tuple
                src += cumulative_node_num
                tgt += cumulative_node_num
                all_edge_indices.src.append(src)
                all_edge_indices.tgt.append(tgt)
            cumulative_node_num += g.get_node_num()
        return all_edge_indices

    all_edge_indices = stack_edge_indices(graphs)
    big_graph.add_edges(all_edge_indices.src, all_edge_indices.tgt)

    # Step 5: Add edge features
    edge_features = dict()
    for g in graphs:
        for feature_name in g.edge_features.keys():
            if feature_name in edge_features:
                edge_features[feature_name].append(g.edge_features[feature_name])
            else:
                edge_features[feature_name] = [g.edge_features[feature_name]]
    for k, v in edge_features.items():
        if None in v:
            continue
        else:
            feature_tensor = torch.cat(v, dim=0)
            big_graph.edge_features[k] = feature_tensor

    # Step 6: Add edge attributes
    total_edge_count = 0
    for g in graphs:
        for i in range(g.get_edge_num()):
            big_graph.edge_attributes[total_edge_count] = g.edge_attributes[i]
            total_edge_count += 1

    # Step 7: Batch information preparation
    big_graph.batch_size = len(graphs)
    def tmp_shucheng_sum_py27(values, start = 0):
        total = start
        for value in values:
            total = total + value
        return total
    big_graph.batch = tmp_shucheng_sum_py27([[i] * graphs[i].get_node_num() for i in range(len(graphs))], start=[])
    big_graph._batch_num_nodes = [g.get_node_num() for g in graphs]
    big_graph._batch_num_edges = [g.get_edge_num() for g in graphs]

    return big_graph


def from_batch(batch: GraphData) -> list:
    """
    Convert a batch consisting of several GraphData instances to a list of GraphData instances.

    Parameters
    ----------
    batch: GraphData
        The source batch to be split.

    Returns
    -------
    list
        A list containing all the GraphData instances contained in the source batch.
    """

    num_nodes = batch._batch_num_nodes
    num_edges = batch._batch_num_edges
    all_edges = batch.get_all_edges()
    batch_size = batch.batch_size
    ret = []
    cum_n_nodes = 0
    cum_n_edges = 0

    # Construct graph respectively
    for i in range(batch_size):
        g = GraphData(device=batch.device)
        g.add_nodes(num_nodes[i])
        edges = all_edges[cum_n_edges:cum_n_edges + num_edges[i]]
        src, tgt = [e[0] - cum_n_nodes for e in edges], [e[1] - cum_n_nodes for e in edges]
        g.add_edges(src, tgt)
        cum_n_edges += num_edges[i]
        cum_n_nodes += num_nodes[i]
        ret.append(g)

    # Add node and edge features

    for k, v in batch._node_features.items():
        if v is not None:
            cum_n_nodes = 0  # Cumulative node numbers
            for i in range(batch_size):
                ret[i].node_features[k] = v[cum_n_nodes:cum_n_nodes + num_nodes[i]]
                cum_n_nodes += num_nodes[i]

    for k, v in batch._edge_features.items():
        if v is not None:
            cum_n_edges = 0  # Cumulative edge numbers
            for i in range(batch_size):
                ret[i].edge_features[k] = v[cum_n_edges:cum_n_edges + num_edges[i]]
                cum_n_edges += num_edges[i]

    cum_n_nodes = 0
    cum_n_edges = 0

    # Add node and edge attributes
    for graph_cnt in range(batch_size):
        for num_graph_nodes in range(num_nodes[graph_cnt]):
            ret[graph_cnt].node_attributes[num_graph_nodes] = batch.node_attributes[cum_n_nodes + num_graph_nodes]
        for num_graph_edges in range(num_edges[graph_cnt]):
            ret[graph_cnt].edge_attributes[num_graph_edges] = batch.edge_attributes[cum_n_edges + num_graph_edges]
        cum_n_edges += num_edges[graph_cnt]
        cum_n_nodes += num_nodes[graph_cnt]

    return ret


# Testing code
if __name__ == '__main__':
    a1 = GraphData()
    a1.add_nodes(10)
    a2 = GraphData()
    a2.add_nodes(15)
    a3 = to_batch([a1, a2])
    print(len(a1.node_attributes), len(a2.node_attributes), len(a3.node_attributes))
