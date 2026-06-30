import argparse
import json
import os
import re
import time

import torch
from tqdm import tqdm
from transformers import AutoTokenizer
from vllm import LLM, SamplingParams

from benchmark import repos
from provider import AnthropicProvider, OpenAIProvider, Provider
from utils import BENCHMARK_PATH, ContextMode, EVAL_RESULT_PATH

USE_API_MODELS = {"deepseek-chat": ("https://api.deepseek.com", "DEEPSEEK_API_KEY"),
                  "anthropic/claude-sonnet-4-6": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
                  "openai/gpt-5.4": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
                  }


class Eval:
    def __init__(self, model_name: str):
        self.model_name = model_name
        self.first = True
        if model_name not in USE_API_MODELS:
            self.tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
            self.model = LLM(model=model_name, trust_remote_code=True, dtype="bfloat16",
                             tensor_parallel_size=torch.cuda.device_count(), gpu_memory_utilization=0.95,
                             enforce_eager=True, max_model_len=32768)
        self.prompt_template = (
            "You are an expert {language} programmer. "
            "Your task is to generate correct and efficient implementation of the function body that replaces [MASK]."
            "You must copy the function signature character by character without any changes, and implement the function body based on the provided description.\n"
            "You should only generate the function code of the specified signature, and ensure that the generated code is syntactically correct and follows best practices for {language} programming.\n"
            "You should only use the provided contexts, and avoid adding any dependencies.\n"
            "Only output the single function with the signature provided, do not add any other code or text outside the function.\n"
            "You should generate the code in a single function, do not split the code into multiple functions or add any helper functions.\n"
            "Wrap the generated function within ```{language} ... ``` code block.\n"
            "\n# Function Description:\n{docstring}\n"
            "\n# Function Signature:\n```{language}\n{signature}\n```"
            "\n# Function Context:\n```{language}\n{contexts}\n```\n"
        )
        self.prompt_no_context_template = (
            "You are an expert {language} programmer. "
            "Your task is to generate correct and efficient implementation of the function body that replaces [MASK]."
            "You must copy the function signature character by character without any changes, and implement the function body based on the provided description.\n"
            "You should only generate the function code of the specified signature, and ensure that the generated code is syntactically correct and follows best practices for {language} programming.\n"
            # "You should only use the provided contexts, and avoid adding any dependencies.\n"
            "Only output the single function with the signature provided, do not add any other code or text outside the function.\n"
            "You should generate the code in a single function, do not split the code into multiple functions or add any helper functions.\n"
            "Wrap the generated function within ```{language} ... ``` code block.\n"
            "\n# Function Description:\n{docstring}\n"
            "\n# Function Signature:\n```{language}\n{signature}\n```"
        )

    @staticmethod
    def request_api(client: Provider, messages, max_tokens: int, temperature: float, n: int,
                    top_p: float | None = None) -> list[str]:
        retry_time = 4
        while True:
            try:
                outputs = client.generate(messages, max_tokens, temperature, n, top_p)
                return outputs
            except Exception as e:
                tqdm.write(f"Error: {e}")
                time.sleep(retry_time)
                retry_time *= 2
                if retry_time > 128:
                    raise e

    def generate_code(self, language: str, docstring: str, contexts: str, signature: str, pass_k: int,
                      mode: ContextMode, max_new_tokens: int = 2048) -> list[tuple[str, str]]:
        match mode:
            case ContextMode.NO_CONTEXT:
                prompt = self.prompt_no_context_template.format(language=language, docstring=docstring,
                                                                signature=signature)
            case ContextMode.WITH_CONTEXT:
                prompt = self.prompt_template.format(language=language, docstring=docstring, contexts=contexts,
                                                     signature=signature)
            case _:
                raise ValueError(f"Unsupported context mode: {mode}")
        messages = [{"role": "user", "content": prompt}]
        if self.model_name in USE_API_MODELS:
            if "claude" in self.model_name:
                client = AnthropicProvider(self.model_name, os.getenv(USE_API_MODELS[self.model_name][1]) or "",
                                           USE_API_MODELS[self.model_name][0])
            else:
                client = OpenAIProvider(self.model_name, os.getenv(USE_API_MODELS[self.model_name][1]) or "",
                                        USE_API_MODELS[self.model_name][0])

            if pass_k == 1:
                outputs = self.request_api(
                    client,
                    messages=messages,
                    max_tokens=max_new_tokens,
                    temperature=0.0,
                    n=1,
                )
            else:
                # response = self.request_api(
                #     messages=messages,
                #     max_tokens=max_new_tokens,
                #     temperature=0.8,
                #     n=pass_k,
                #     top_p=0.95
                # ) # n未必支持，使用手动循环
                outputs = self.request_api(
                    client,
                    messages=messages,
                    max_tokens=max_new_tokens,
                    temperature=0.8,
                    n=pass_k,
                    top_p=0.95,
                )
        else:
            inputs = self.tokenizer.apply_chat_template(messages, add_generation_prompt=True, tokenize=False,
                                                        enable_thinking=False)
            if pass_k == 1:
                params = SamplingParams(
                    max_tokens=max_new_tokens,
                    temperature=0.0
                )
            else:
                params = SamplingParams(
                    max_tokens=max_new_tokens,
                    temperature=0.8,
                    top_p=0.95,
                    n=pass_k
                )
            outputs = self.model.generate(inputs, sampling_params=params, use_tqdm=False)
            outputs = outputs[0].outputs

        generated_codes = []
        for i in range(pass_k):
            if self.model_name in USE_API_MODELS:
                generated_code = outputs[i]
            else:
                generated_code = outputs[i].text
            # 提取所有``` ... ```中的代码
            matches = re.findall(r"```(.*?)```", generated_code, re.DOTALL)
            if matches:
                code = ""
                for match in matches:
                    cur_code = match.strip()
                    if cur_code.lower().startswith(language.lower()):
                        cur_code = cur_code[len(language):].strip()
                    code += cur_code + "\n"
                code = code.strip()
                generated_codes.append((generated_code, code))
                continue
            # 如果没有匹配到，说明至多存在一个```标记
            if "```" in generated_code:
                parts = generated_code.split("```")
                assert len(parts) < 3, "Unexpected number of ``` in generated code"
                if len(parts) == 2:
                    # 只有一个```，取其后的部分作为代码
                    code = parts[1].strip()
                    if code.lower().startswith(language.lower()):
                        code = code[len(language):].strip()
                    generated_codes.append((generated_code, code))
                    continue
            tqdm.write(f"Warning: No code block found for language {language}. Returning full generated text.")
            tqdm.write(f"Generated text:\n{generated_code}")
            generated_codes.append((generated_code, generated_code.strip()))

        if self.first:
            tqdm.write(f"Benchmark path: {BENCHMARK_PATH}, Eval result path: {EVAL_RESULT_PATH}")
            tqdm.write(f"Model: {self.model_name}, Pass@{pass_k}, Context Mode: {mode.value}")
            tqdm.write(f"Prompt:\n{prompt}")
            if self.model_name in USE_API_MODELS:
                tqdm.write(f"Response:\n{outputs}")
            else:
                tqdm.write(f"Response:\n{[output.text for output in outputs]}")
            self.first = False
        return generated_codes

    def eval(self, languages: list[str], model_name: str, pass_k: int, mode: ContextMode):
        error = False
        for language in languages:
            language_path = os.path.join(BENCHMARK_PATH, language)
            if not os.path.isdir(language_path):
                continue
            for file_name in os.listdir(language_path):
                if not file_name.endswith(".jsonl"):
                    continue
                file_path = os.path.join(language_path, file_name)
                with open(file_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                repo_path = f"repos/{language}/{file_name[:-6]}"  # 去除.jsonl后缀
                results = []
                result_path = f"{EVAL_RESULT_PATH}/{model_name.split('/')[-1]}/pass@{pass_k}/" + "/".join(
                    file_path.split("/")[1:])
                if mode != ContextMode.WITH_CONTEXT:
                    result_path = f"{mode.value}/" + result_path
                if os.path.exists(result_path):
                    continue
                for line in tqdm(lines, desc=f"Evaluating {file_name}"):
                    func = json.loads(line)
                    try:
                        code_list = self.generate_code(language, func["docstring"], func["contexts"], func["signature"],
                                                       pass_k, mode)
                    except Exception as e:
                        import traceback
                        tqdm.write(
                            f"Error generating code for function {func['name']} in file {func['file_path']}: {e}")
                        tqdm.write(traceback.format_exc())
                        code_list = []
                        for i in range(pass_k):
                            code_list.append(("__GENERATE_CODE_ERROR__", "__GENERATE_CODE_ERROR__"))
                        error = True

                    for response, code_generated in code_list:
                        results.append({
                            "name": func["name"],
                            "file_path": func["file_path"],
                            "signature": func["signature"],
                            "docstring": func["docstring"],
                            "contexts": func["contexts"],
                            "code": func["code"],
                            "hash": func["hash"],
                            "response": response,
                            "code_generated": code_generated,
                            "tests": func.get("tests", [])
                        })
                    if error:
                        break
                os.makedirs(os.path.dirname(result_path), exist_ok=True)
                with open(result_path, "w", encoding="utf-8") as f:
                    for result in results:
                        while True:
                            try:
                                f.write(json.dumps(result, ensure_ascii=False) + "\n")
                                break
                            except Exception as e:
                                tqdm.write(
                                    f"Error writing result for function {result['name']} in file {result['file_path']}: {e}")
                                time.sleep(300)
                if error:
                    return


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model_name", type=str, required=True, help="Name of the model to evaluate.")
    parser.add_argument("--languages", type=str, nargs='+', default=["all"],
                        help="List of programming languages to evaluate, or 'all' for all supported languages.")
    parser.add_argument("--pass-k", type=int, default=1, help="Top-K code generations to consider for evaluation.")
    parser.add_argument("--mode", type=str, choices=ContextMode._value2member_map_.keys(),
                        default=ContextMode.WITH_CONTEXT.value, help="Whether to include context in the prompt.")
    parser.add_argument("--cdd", action="store_true", default=False)
    parser.add_argument("--benchmark_path", type=str, default=BENCHMARK_PATH, help="Path to the benchmark dataset.")
    parser.add_argument("--eval_result_path", type=str, default=EVAL_RESULT_PATH,
                        help="Path to the evaluation results.")
    args = parser.parse_args()
    mode = ContextMode(args.mode)
    BENCHMARK_PATH = args.benchmark_path
    EVAL_RESULT_PATH = args.eval_result_path

    if "all" in args.languages:
        selected_languages = list(repos.keys())
    else:
        selected_languages = [lang.lower() for lang in args.languages]

    if args.cdd:
        args.pass_k = 50
        EVAL_RESULT_PATH = "cdd/eval_results"

    if mode == ContextMode.BM25:
        BENCHMARK_PATH = "benchmark_bm25"
        EVAL_RESULT_PATH = "bm25/eval_results"
        mode = ContextMode.WITH_CONTEXT

    evaluate = Eval(args.model_name)
    evaluate.eval(selected_languages, args.model_name, args.pass_k, mode)
