import subprocess, time, unittest
from subprocess import PIPE, STDOUT
import numpy as np
import torch

class DotCompressor(torch.nn.Module):
    def __init__(self, in_features, out_features, weights=None):
        super(DotCompressor, self).__init__()
        self.projected_rtc_layer = torch.nn.Linear(in_features, out_features, bias=True)
        self.h_channel_dim = out_features
        if weights is not None:
            assert len(weights) == 2
            # weights shape  (out_features,in_features)
            # bias shape (out_features)
            self.projected_rtc_layer.weight = torch.nn.Parameter(torch.from_numpy(weights[0]), requires_grad=True)
            self.projected_rtc_layer.bias = torch.nn.Parameter(torch.from_numpy(weights[1]), requires_grad=True)

    def forward(self, dense_embeddings, sparse_embeddings, dense_projection, debug=False):
        assert len(dense_projection.shape) == 2
        assert len(dense_embeddings[0].shape) == 2
        assert len(sparse_embeddings[0].shape) == 2
        assert len(dense_embeddings) > 0, 'embeddings to be compressed can not be empty'
        assert len(sparse_embeddings) > 0, 'embeddings to be compressed can not be empty'
        num_channels = dense_embeddings.shape[1] + sparse_embeddings.shape[1]
        batch_size = dense_embeddings.shape[0]
        in_dim = dense_embeddings.shape[2]
        # concat embeddings
        cat_input = torch.cat((dense_embeddings, sparse_embeddings), dim=1)
        if debug:
            print("concatenated inputs", cat_input.shape)
        # reshape to add channel dimension
        cat_input_reshape = cat_input.reshape(batch_size, num_channels, in_dim)
        if debug:
            print('reshaped input', cat_input_reshape.shape)
        # transpose
        transpose_cat = torch.transpose(cat_input_reshape, 2, 1)
        if debug:
            print('transposed', transpose_cat.shape)
        # reshape 3 to 2
        batched_input_size = batch_size * in_dim
        reshape_transpose_cat = torch.reshape(transpose_cat, (batched_input_size, num_channels))
        if debug:
            print('reshaped', reshape_transpose_cat.shape)
        # linear layer
        projected_rtc = self.projected_rtc_layer(reshape_transpose_cat.double())
        if debug:
            print('projected batch:', projected_rtc.shape)
        # unpack inputs reshape 2 to 3
        unpacked_projected_rtc = torch.reshape(projected_rtc, (batch_size, i_dim, self.h_channel_dim))
        if debug:
            print('unpacked_projected_rtc', unpacked_projected_rtc.shape)
        # bmm
        batch_pairwise = torch.bmm(transpose_cat.transpose(2, 1).float(), unpacked_projected_rtc.float())
        if debug:
            print('bmm input1', transpose_cat.shape, 'bmm input2', unpacked_projected_rtc.shape)
            print('batch_pairwise', batch_pairwise.shape)
        flattened_pairwise = batch_pairwise.flatten(1, 2)
        if debug:
            print('flattened_pairwise', flattened_pairwise.shape)
        tanh_flatteded_pairwise = torch.tanh(flattened_pairwise)
        if debug:
            print('tanh_flatteded_pairwise', tanh_flatteded_pairwise.shape)
        if debug:
            print('dense_projection', dense_projection.shape)
        cat_compression_ret = torch.cat([tanh_flatteded_pairwise, dense_projection], 1)
        return cat_compression_ret

def dump_tensor_to_file(tensor, file_name):
    buffer = []
    for entry in tensor.flatten():
      buffer.append(entry)
    buffer = ["%.16f"%x for x in buffer]
    with open(file_name, 'w+') as f:
      f.write(' '.join(buffer))

def batch_matmul_3d_reference(input1, input2, trans1, trans2):
    '''
    Input layout:
    input1 (d,k,m)
    input2 (d,k,n)
    output (d,m,n)
    '''
    input1 = input1.transpose((0,2,1)) if trans1 else input1
    input2 = input2.transpose((0,2,1)) if trans2 else input2
    return np.matmul(input1, input2)

def batch_transpose_3d_reference(input):
    '''
    This operation transposes the inner 2 dimensions (flip inner two)
    and assumes tensor outter dimension is sample dimension
    '''
    return input.transpose((0,2,1))

def gen_FF_result(test_target, num_gpu):
    command = 'cd ~/DLRM_FlexFlow/src/ops/tests/ && ./test_run_FF_target.sh %s %s' % (test_target, str(num_gpu))
    test_process = subprocess.Popen([command], shell=True, stdin=PIPE, stdout=PIPE, stderr=STDOUT)
    test_process.stdout.read()
    test_process.wait()

def is_equal_tensor_from_file(file_1, file_2, label='', epsilon=0.00001):
    with open(file_1, 'r') as f:
        input1 = f.readline()
    with open(file_2, 'r') as f:
        input2 = f.readline()
    input1_flat = input1.strip().split(' ')
    input1_flat = [float(x) for x in input1_flat]
    input2_flat = input2.strip().split(' ')
    input2_flat = [float(x) for x in input2_flat]
    try:
      np.testing.assert_allclose(input1_flat, input2_flat, rtol=epsilon, atol=0.0)
    except Exception as e:
      print('checking equal %s failed, error message: %s' % (label, e))
      raise e 

class DotCompressorTest(unittest.TestCase):
    '''
    Dot Compressor shapes:
    input1: list of 2d tensors (batch_size, i_dim), the size of list is channel dimension
    input2: another 2d tensor that we want to concat with the projected input1, input2 shape (batch_size, i2_dim)
    output: in shape 
    (batch_size, i_dim * out_channel_dim + i2_dim)
    example:
    ```
      batch_size = 145
      i_dim = 64
      num_channels = 265
      projected_num_channels = 15
      dense_projection_i_dim = 512
      linear_weight = np.random.uniform(0,1, (projected_num_channels, num_channels, ))
      linear_bias = np.random.uniform(0,1, projected_num_channels)

      dense_projection = np.random.uniform(0, 1, (batch_size, dense_projection_i_dim))
      dense_projection = torch.from_numpy(dense_projection).float()
      chunk_dense_embedded = np.random.uniform(0, 1, (batch_size, num_channels // 2, i_dim))
      chunk_sparse_embedded = np.random.uniform(0, 1, (batch_size, num_channels - num_channels // 2, i_dim))
      print('chunk dense embedded', chunk_dense_embedded.shape, "chunk sparse embedded", chunk_sparse_embedded.shape)
      chunk_dense_embedded = torch.from_numpy(chunk_dense_embedded)
      chunk_sparse_embedded = torch.from_numpy(chunk_sparse_embedded)
      pretrained_weights = [linear_weight, linear_bias]
      m = DotCompressor(num_channels, projected_num_channels, pretrained_weights)
      ret = m(chunk_dense_embedded, chunk_sparse_embedded, dense_projection, debug=False)
      print(num_channels, chunk_dense_embedded.shape, chunk_sparse_embedded.shape, dense_projection.shape)
      print(ret.shape)
    ```
    '''

class BatchMatmulTest(unittest.TestCase):
    '''
    BMM default layout
    input1 (d,m,k)
    input2 (d,k,n)
    output (d,m,n)

    Target shape in caffe2
    input1 (d,k,m)
    input2 (d,k,n)
    output (d,m,n)
    so we set trans1=True and trans2=False
    '''
    TEST_TARGET = 'batch_matmul_test'
    def _dump_meta(self, m,k,n,d):
        with open('test_meta.txt', 'w+') as f:
          f.write(' '.join([str(m), str(k), str(n), str(d)]))

    def _run_gpu_test(self, num_gpu, d, m, n, k, epsilon=0.00001):
        # generate python reference and input payload
        input1_tensor = np.random.uniform(0, 1, (d,k,m))
        dump_tensor_to_file(input1_tensor, "test_input1.txt")
        input2_tensor = np.random.uniform(0, 1, (d,k,n))
        dump_tensor_to_file(input2_tensor, "test_input2.txt")
        output_gradient_tensor = np.random.uniform(0, 1, (d,m,n))
        dump_tensor_to_file(output_gradient_tensor, "test_output_grad.txt")

        output_tensor = batch_matmul_3d_reference(input1_tensor, input2_tensor, trans1=True, trans2=False)
        input1_grad_tensor = batch_matmul_3d_reference(input2_tensor, output_gradient_tensor, trans1=False, trans2=True)
        input2_grad_tensor = batch_matmul_3d_reference(input1_tensor, output_gradient_tensor, trans1=False, trans2=False)
        dump_tensor_to_file(output_tensor, "test_output.txt")
        dump_tensor_to_file(input1_grad_tensor, "test_input1_grad.txt")
        dump_tensor_to_file(input2_grad_tensor, "test_input2_grad.txt")
        self._dump_meta(m,k,n,d)

        # generate FF results
        gen_FF_result(BatchMatmulTest.TEST_TARGET, num_gpu)
        file1 = 'output.txt'
        file2 = 'test_output.txt'
        ret1 = is_equal_tensor_from_file(file1, file2, 'output', epsilon=epsilon)
        file1 = 'test_input1_grad.txt'
        file2 = 'input1_grad.txt'
        ret2 = is_equal_tensor_from_file(file1, file2, 'input1_grad', epsilon=epsilon)
        file1 = 'test_input2_grad.txt'
        file2 = 'input2_grad.txt'
        ret3 = is_equal_tensor_from_file(file1, file2, 'input2_grad', epsilon=epsilon)

    def test_single_gpu_single_batch(self):
        # generate test payload
        d,m,n,k = 1,2,3,4
        num_gpu = 1
        self._run_gpu_test(num_gpu, d, m, n, k)
    
    def test_single_gpu_multi_batches(self):
        # generate test payload
        d,m,n,k = 5,2,3,4
        num_gpu = 1
        self._run_gpu_test(num_gpu, d, m, n, k)

    def test_multi_gpus_multi_batches(self):
        # generate test payload
        d,m,n,k = 5,2,3,4
        num_gpu = 2
        self._run_gpu_test(num_gpu, d, m, n, k)

    # def uneven_distribute_test(self):
    #     # for this configuration we can't distribute payload to each GPU because
    #     # ceil(9 / 8) = 2, for each gpu we assign 2 batches, such we only assign payloads to 5 gpus, 3 gpus won't get 
    #     # any payload, in this scenario FF throws a  `acc.accessor.is_dense_arbitrary(rect)' failed error
    #     # this error is too deep for user to debug, we need to handle this case in FF 
    #     # and throw proper exception - so this test should expect a exception
    #     d,m,n,k = 9,2,3,4
    #     num_gpu = 8
    #     self._run_gpu_test(num_gpu, d, m, n, k)

    def test_unit_size_matrix(self):
        # generate test payload
        d,m,n,k = 1,1,1,1
        num_gpu = 1
        self._run_gpu_test(num_gpu, d, m, n, k)
    
    def test_unit_size_matrix(self):
        # generate test payload
        d,m,n,k = 2,1,1,1
        num_gpu = 2
        self._run_gpu_test(num_gpu, d, m, n, k)

    def test_small_size_matrix2(self):
        # generate test payload
        d,m,n,k = 2,2,2,1
        num_gpu = 2
        self._run_gpu_test(num_gpu, d, m, n, k)

    def test_multi_gpus_ads_team_target_model_shape(self):
        # generate test payload
        d,m,n,k = 145,265,15,64
        num_gpu = 2
        ret = self._run_gpu_test(num_gpu, d, m, n, k, epsilon=0.0001)

    def test_single_gpu_ads_team_target_model_shape(self):
        # generate test payload
        d,m,n,k = 145,265,15,64
        num_gpu = 1
        ret = self._run_gpu_test(num_gpu, d, m, n, k, epsilon=0.0001)

class TransposeTest(unittest.TestCase):
    '''
    Transpose shape (d,m,k)
    '''
    TEST_TARGET = 'transpose_test'
    def _dump_meta(self,m,k,d):
        with open('test_meta.txt', 'w+') as f:
          f.write(' '.join([str(m), str(k), str(d)]))

    def test_single_gpu_single_batch(self):
        # generate test payload
        d,m,k = 1,2,3
        num_gpu = 1
        self._run_gpu_test(num_gpu, d, m, k)

    def test_single_gpu_multi_batches(self):
        d,m,k = 9,2,3
        num_gpu = 1
        self._run_gpu_test(num_gpu, d, m, k)
    
    def test_unit_batch_matrix(self):
        d,m,k = 1,1,1
        num_gpu = 1
        self._run_gpu_test(num_gpu, d, m, k)
      
    def test_multi_gpus_ads_team_target_shape(self):
        d,m,k = 145, 265, 64
        num_gpu = 2
        self._run_gpu_test(num_gpu, d, m, k)

    def test_single_gpu_ads_team_target_shape(self):
        d,m,k = 145, 265, 64
        num_gpu = 1
        self._run_gpu_test(num_gpu, d, m, k)

    def test_multi_gpus_small_problem(self):
        d,m,k = 2,3,4
        num_gpu = 2
        self._run_gpu_test(num_gpu, d, m, k)
    
    def uneven_split_multi_gpus_multi_batch(self):
        d,m,k = 3,4,5
        num_gpu = 2
        self._run_gpu_test(num_gpu, d, m, k)

    # # if number_gpu * number_node > batch_size will throw exception
    # # need to handle this exception in FF and add this unit test later on (to expect an exception)
    # def test_multi_gpus_single_batch(self):
    #     d,m,k = 1,2,3
    #     num_gpu = 2
    #     ret = self.transpose_test_pipeline(num_gpu, d, m, k)

    def _run_gpu_test(self, num_gpu, d, m, k, epsilon=0.00001):
        # generate python reference and input payload
        input_tensor = np.random.uniform(0, 1, (d,m,k))
        dump_tensor_to_file(input_tensor, "test_input1.txt")
        output_gradient_tensor = np.random.uniform(0, 1, (d,k,m))
        dump_tensor_to_file(output_gradient_tensor, "test_output_grad.txt")
        output_tensor = batch_transpose_3d_reference(input_tensor)
        input_grad_tensor = batch_transpose_3d_reference(output_gradient_tensor)
        dump_tensor_to_file(output_tensor, "test_output.txt")
        dump_tensor_to_file(input_grad_tensor, "test_input1_grad.txt")
        self._dump_meta(m,k,d)

        # generate FF results
        gen_FF_result(TransposeTest.TEST_TARGET, num_gpu)
        file1 = 'output.txt'
        file2 = 'test_output.txt'
        is_equal_tensor_from_file(file1, file2, 'output', epsilon=epsilon)
        file1 = 'test_input1_grad.txt'
        file2 = 'input1_grad.txt'
        is_equal_tensor_from_file(file1, file2, 'input_grad', epsilon=epsilon)

class ReshapeTest(unittest.TestCase):
    '''
    4 dimensional to 2dimensional flatten
    '''
    TEST_TARGET = 'reshape_test'
    def _dump_meta(self,i_dim, o_dim, i_shape, o_shape):
        i_shape = [str(x) for x in i_shape]
        o_shape = [str(x) for x in o_shape]
        with open('test_meta.txt', 'w+') as f:
          f.write(' '.join([str(i_dim), str(o_dim)]+i_shape+o_shape))

   
    def _run_gpu_test(self, num_gpu, i_dim, o_dim, i_shape, o_shape, epsilon=0.00001):
        # generate python reference and input payload
        input_tensor = np.random.uniform(0, 1, i_shape)
        dump_tensor_to_file(input_tensor, "test_input1.txt")
        output_gradient_tensor = np.random.uniform(0, 1, o_shape)
        dump_tensor_to_file(output_gradient_tensor, "test_output_grad.txt")
        output_tensor = input_tensor.reshape(o_shape)
        input_grad_tensor = output_gradient_tensor.reshape(i_shape)
        dump_tensor_to_file(output_tensor, "test_output.txt")
        dump_tensor_to_file(input_grad_tensor, "test_input1_grad.txt")
        self._dump_meta(i_dim, o_dim, i_shape, o_shape)

        # generate FF results
        gen_FF_result(ReshapeTest.TEST_TARGET, num_gpu)
        file1 = 'output.txt'
        file2 = 'test_output.txt'
        is_equal_tensor_from_file(file1, file2, 'output', epsilon=epsilon)
        file1 = 'test_input1_grad.txt'
        file2 = 'input1_grad.txt'
        is_equal_tensor_from_file(file1, file2, 'input_grad', epsilon=epsilon)

      
    def test_single_gpu_multi_batch_32(self):
        num_gpu = 1
        i_dim = 3
        o_dim = 2
        i_shape = (3,3,15)
        o_shape = (9,15)
        self._run_gpu_test(num_gpu, i_dim, o_dim, i_shape, o_shape)

    def test_multi_gpu_multi_batch_32(self):
        num_gpu = 2
        i_dim = 3
        o_dim = 2
        i_shape = (2,3,4)
        o_shape = (6,4)
        self._run_gpu_test(num_gpu, i_dim, o_dim, i_shape, o_shape)

    def test_problem_size_32(self):
        num_gpu = 2
        i_dim = 3
        o_dim = 2
        i_shape = (144,64,265)
        o_shape = (144*64,265)
        self._run_gpu_test(num_gpu, i_dim, o_dim, i_shape, o_shape)


    def test_single_gpu_multi_batch_23(self):
        num_gpu = 1
        i_dim = 2
        o_dim = 3
        i_shape = (9,15)
        o_shape = (3,3,15)
        self._run_gpu_test(num_gpu, i_dim, o_dim, i_shape, o_shape)

    def test_multi_gpu_multi_batch_23(self):
        num_gpu = 2
        i_dim = 2
        o_dim = 3
        i_shape = (6,2)
        o_shape = (2,3,2)
        self._run_gpu_test(num_gpu, i_dim, o_dim, i_shape, o_shape)

    def test_problem_size_23(self):
        num_gpu = 2
        i_dim = 2
        o_dim = 3
        i_shape = (144*64,265)
        o_shape = (144,64,265)
        self._run_gpu_test(num_gpu, i_dim, o_dim, i_shape, o_shape)
    
    def test_flatten_inner_2(self):
        num_gpu = 2
        i_dim = 3
        o_dim = 2
        i_shape = (2,3,2)
        o_shape = (2,6)
        self._run_gpu_test(num_gpu, i_dim, o_dim, i_shape, o_shape) 
      
    def test_flatten_inner_2_target_size(self):
        num_gpu = 2
        i_dim = 3
        o_dim = 2
        i_shape = (144,265,15)
        o_shape = (144,3975)
        self._run_gpu_test(num_gpu, i_dim, o_dim, i_shape, o_shape)

class TanhTest(unittest.TestCase):
    '''
    4 dimensional to 2dimensional flatten
    '''
    TEST_TARGET = 'tanh_test'
    def _dump_meta(self,i_dim, o_dim, i_shape, o_shape):
        i_shape = [str(x) for x in i_shape]
        o_shape = [str(x) for x in o_shape]
        with open('test_meta.txt', 'w+') as f:
          f.write(' '.join([str(i_dim), str(o_dim)]+i_shape+o_shape))

   
    def _run_gpu_test(self, num_gpu, i_dim, o_dim, i_shape, epsilon=0.00001):
        # generate python reference and input payload
        input_tensor_value = np.random.uniform(0, 1, i_shape)
        input_tensor = torch.from_numpy(input_tensor_value).float()
        dump_tensor_to_file(input_tensor_value, "test_input1.txt")
        output_gradient_tensor = np.random.uniform(0, 1, i_shape)
        dump_tensor_to_file(output_gradient_tensor, "test_output_grad.txt")
        output_tensor = torch.nn.Tanh()(input_tensor).numpy()
        dump_tensor_to_file(output_tensor, "test_output.txt")
        self._dump_meta(i_dim, o_dim, i_shape, i_shape)

        # generate FF results
        gen_FF_result(TanhTest.TEST_TARGET, num_gpu)
        file1 = 'output.txt'
        file2 = 'test_output.txt'
        is_equal_tensor_from_file(file1, file2, 'output', epsilon=epsilon)

        # todo: add backward checking
        # todo: tanh backward
        # input_grad_tensor = output_gradient_tensor.reshape(i_shape)
        # dump_tensor_to_file(input_grad_tensor, "test_input1_grad.txt")
        # file1 = 'test_input1_grad.txt'
        # file2 = 'input1_grad.txt'
        # is_equal_tensor_from_file(file1, file2, 'input_grad', epsilon=epsilon)

      
    def test_single_gpu_multi_batch_3d(self):
        num_gpu = 1
        i_dim = 3
        i_shape = (2,3,5)
        self._run_gpu_test(num_gpu, i_dim, i_dim, i_shape)

    def test_single_gpu_multi_batch_2d(self):
        num_gpu = 1
        i_dim = 2
        i_shape = (2,3)
        self._run_gpu_test(num_gpu, i_dim, i_dim, i_shape)

    def test_multi_gpu_multi_batch_2d(self):
        num_gpu = 2
        i_dim = 2
        i_shape = (2,3)
        self._run_gpu_test(num_gpu, i_dim, i_dim, i_shape)

    def test_multi_gpu_target_size(self):
        num_gpu = 2
        i_dim = 2
        i_shape = (145,3975)
        self._run_gpu_test(num_gpu, i_dim, i_dim, i_shape)

    def test_multi_gpu_multi_batch_3d(self):
        num_gpu = 2
        i_dim = 3
        i_shape = (2,2,2)
        self._run_gpu_test(num_gpu, i_dim, i_dim, i_shape)

if __name__ == '__main__':
    unittest.main()
