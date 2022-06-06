import torch
import warnings

from torch.nn import Module


class NNUtil(Module):
    r"""
    Utility for nearest neighbor search. It would first try to use `faiss`_ (requiring separate pacakge installment)
    as the backend for better computational performance. Otherwise, `scikit-learn` would be used as it is pre-installed
    with gpytorch.

    :param int k: number of nearest neighbors
    :param int dim: dimensionality of data
    :param torch.Size batch_shape: batch shape for train data
    :param str preferred_nnlib: currently supports `faiss` and `scikit-learn`.

    Example:
        >>> train_x = torch.randn(10, 5)
        >>> nn_util = NNUtil(k=3, dim=train_x.size(-1))
        >>> nn_util.set_nn_idx(train_x)
        >>> test_x = torch.randn(2, 5)
        >>> test_nn_indices = nn_util.find_nn_idx(test_x) # finding 3 nearest neighbors for test_x
        >>> test_nn_indices = nn_util.find_nn_idx(test_x, k=2) # finding 2 nearest neighbors for test_x
        >>> sequential_nn_idx = nn_util.build_sequential_nn_idx(train_x) # build up sequential nearest neighbor structure for train_x

    .. _faiss:
    https://github.com/facebookresearch/faiss
    """

    def __init__(self, k, dim, batch_shape=torch.Size([]), preferred_nnlib="faiss", device='cpu'):
        super().__init__()
        assert k > 0, f"k must be greater than 0, but got k = {k}."
        self.k = k
        self.dim = dim
        assert isinstance(batch_shape, torch.Size), \
            f"batch_shape must be an instance of torch.Size, but got {type(batch_shape)}"
        self.batch_shape = batch_shape

        self.train_n = None

        if preferred_nnlib == "faiss":
            try:
                import faiss
                import faiss.contrib.torch_utils
                self.nnlib = "faiss"
                self.cpu()  # Initializes the index

            except ImportError:
                warnings.warn(
                    "Tried to import faiss, but failed. Falling back to scikit-learn NN.",
                    ImportWarning
                )
                self.nnlib = "sklearn"

        else:
            self.nnlib = "sklearn"

        self.to(device)

    def cuda(self, device=None):
        super().cuda(device=device)
        if self.nnlib == "faiss":
            from faiss import StandardGpuResources, GpuIndexFlatL2
            self.res = StandardGpuResources()
            self.index = [GpuIndexFlatL2(self.res, self.dim) for _ in range(self.batch_shape.numel())]
        return self

    def cpu(self):
        super().cpu()
        if self.nnlib == "faiss":
            from faiss import IndexFlatL2
            self.res = None
            self.index = [IndexFlatL2(self.dim) for _ in range(self.batch_shape.numel())]
        return self

    def find_nn_idx(self, test_x, k=None):
        """
        Find :math:`k` nearest neighbors for test data `test_x` among the training data stored in this utility
        :param test_x: test data, shape (*batch_shape, test_n, dim)
        :param k: number of nearest neighbors. Default is the value used in utility initialization. If users specify
        a different value, it needs to be smaller than or equal to the initialized value.
        :return: the indices of nearest neighbors in the training data
        """

        assert self.train_n is not None, "Please initialize with training data first."
        if k is None:
            k = self.k
        else:
            assert k > 0, f"k must be greater than 0, but got k = {k}."
        assert k <= self.train_n, f"k should be smaller than number of train data, " \
                                  f"but got k = {k}, number of train data = {self.train_n}."

        test_x = self._expand_and_check_shape(test_x)

        with torch.no_grad():
            if self.nnlib == 'sklearn':
                # TODO: check this
                if self.neighbors is None:
                    raise RuntimeError("The nearest neighbor set has not been defined. First call `set_nn_idx`")
                nn_idx = torch.from_numpy(self.train_neighbors.kneighbors(test_x.cpu().numpy())[1])
                nn_idx = nn_idx.long().to(test_x.device)
            else:
                test_n = test_x.shape[-2]
                test_x = test_x.view(-1, test_n, self.dim)

                nn_idx = torch.empty(self.batch_shape.numel(), test_n, k, dtype=torch.int64)

                for i in range(self.batch_shape.numel()):
                    nn_idx[i] = self.index[i].search(test_x[i], k)[1]
                nn_idx = nn_idx.view(*self.batch_shape, test_n, k)
        return nn_idx

    def set_nn_idx(self, train_x):
        """
        Set the indices of training data to facilitate nearest neighbor search. If users want to perform nearest
        neighbor search on new training data, this function needs to be called again.
        :param train_x: training data points, shape (*batch_shape, size, dim)
        :return: None
        """
        train_x = self._expand_and_check_shape(train_x)
        self.train_n = train_x.shape[-2]

        with torch.no_grad():
            if self.nnlib == 'sklearn':
                import sklearn.neighbors as NearestNeighbors
                x = train_x.cpu().numpy()
                # TODO: fix this
                self.neighbors = NearestNeighbors(n_neighbors=self.k + 1, algorithm='auto').fit(x)
            elif self.nnlib == "faiss":
                train_x = train_x.view(-1, self.train_n, self.dim)
                for i in range(self.batch_shape.numel()):
                    self.index[i].reset()
                    self.index[i].add(train_x[i])

    def build_sequential_nn_idx(self, x):
        r"""
        Build the sequential :math:`k` nearest neighbor structure within training data in the folloiwng way:
        for the :math:`i`-th data point :math:`x_i`, find its :math:`k` nearest neighbors among preciding
        training data :math:`x_1, \cdots, x_{i-1}`, for `i=k+1:N` where `N` is the size of training data.
        :param x: training data. Shape `(N, D)`
        :return: indices of nearest neighbors. Shape: `(N-k, k)`
        """
        x = self._expand_and_check_shape(x)
        N = x.shape[-2]
        assert self.k < N, f"k should be smaller than number of data, but got k = {self.k}, number of data = {N}."

        if self.nnlib == 'faiss':
            from faiss import IndexFlatL2

            # building nearest neighbor structure within inducing points
            index = IndexFlatL2(self.dim)
            with torch.no_grad():
                if self.res is not None:
                    from faiss import index_cpu_to_gpu
                    index = index_cpu_to_gpu(self.res, 0, index)

                N = x.shape[-2]
                nn_idx = torch.empty(self.batch_shape.numel(), N - self.k, self.k, dtype=torch.int64)
                x_np = x.view(-1, N, self.dim).data.float().cpu().numpy()

                for bi in range(self.batch_shape.numel()):
                    index.reset()
                    index.add(x_np[bi][:self.k])
                    for i in range(self.k, N):
                        row = x_np[bi][i][None, :]
                        nn_idx[bi][i-self.k].copy_(
                            torch.from_numpy(index.search(row, self.k)[1][..., 0,:]).long().to(x.device)
                        )
                        index.add(row)

                nn_idx = nn_idx.view(*self.batch_shape, N-self.k, self.k)
        else:
            assert self.nnlib == 'sklearn'
            pass
        return nn_idx

    def to(self, device=None):
        """
        Put the utility to a cpu or gpu device
        """
        if device == "cpu":
            return self.cpu()
        elif "cuda" in str(device):
            return self.cuda()
        else:
            raise ValueError(f"Unknown device {device}")

    def _expand_and_check_shape(self, x):
        if len(x.shape) == 1:
            x = x.unsqueeze(-1)  # add dimension
        assert x.shape[:-2] == self.batch_shape, f"x's batch shape must be equal to self.batch_shape, " \
                                                 f"but got x's batch shape={x.shape[:-2]}, self.batch_shape={self.batch_shape}."
        assert x.shape[-1] == self.dim, f"x's dim must be equal to self.dim, " \
                                         f"but got x's dim = {x.shape[-1]}, self.dim = {self.dim}"
        return x