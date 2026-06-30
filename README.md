# LOREBench: A Repository-Level Code Generation Benchmark for Low-Resource Languages

## Directory Structure

- src: source code for the benchmark, including data processing, evaluation, and execution.
- benchmark: benchmark datasets.
- eval_results: evaluation results of the benchmark.
- exec_results: execution results of the benchmark.
- repos.tar.gz: compressed file containing the repositories used in the benchmark construction, which can be unpacked to
  get the repositories if you don't want to clone them from GitHub.

## Environment Setup

1. Install Python 3.10.4.
2. Install torch.
3. Install dependencies using pip.

```bash
pip install -r src/requirements.txt
```

3. Build the image for benchmark construction and execution. It is recommended to use the same username to build the
   image to ensure consistent user permissions between the host and the container, which helps avoid permission issues
   when mounting directories.
```bash
docker build \
  --build-arg USERNAME=$(whoami) \
  --build-arg UID=$(id -u) \
  --build-arg GID=$(id -g) \
  -t lorebench src
```

## Benchmark Construction

```bash
python src/benchmark.py
```

The benchmark construction process will automatically clone the repositories from GitHub using SSH, so please make sure
you have set up SSH keys for GitHub access, or you can unpack the `repos.tar.gz` file to get the repositories. You need
to put the repositories in the `repos/` directory before running the benchmark construction script.

The commit hash of the repositories used in the benchmark construction is recorded in `src/utils.py`. You can clone the repositories to the specified commit hash to ensure consistency with the benchmark construction.

The constructed benchmark will be saved in the `benchmark/` directory

## Code Generation

If you want to use local models, please set the `model_name` to the path of the local model.

We also implemented the OpenAI/Anthropic API interface, so you can modify the URL and API key in `src/eval.py` to use the OpenAI/Anthropic API for evaluation.

```bash
python src/eval.py --model_name <model_name> --pass-k <k>
```

The evaluation results will be saved in the `eval_results/<model_name>/<pass@k>/` directory.

## Execution

```bash
python src/exec.py --model_name <model_name> --pass-k <k>
```

The execution results will be saved in the `exec_results/<model_name>/<pass@k>/` directory.

And you can use the following command to calculate the execution accuracy based on the execution results.

```bash
python src/statistic.py --model_name <model_name> --k <k>
```