import anthropic
import openai


class Provider:
    def __init__(self):
        pass

    def generate(self, messages, max_tokens: int, temperature: float, n: int,
                 top_p: float | None):
        raise NotImplementedError


class OpenAIProvider(Provider):
    def __init__(self, model_name: str, api_key: str, url: str):
        super().__init__()
        self.model_name = model_name
        self.client = openai.OpenAI(api_key=api_key, base_url=url)

    def generate(self, messages, max_tokens: int, temperature: float, n: int,
                 top_p: float | None):
        completion = self.client.chat.completions.create(
            model=self.model_name,
            stream=False,
            messages=messages,
            # max_tokens=max_tokens,    # max_tokens is not supported with gpt-5.4
            max_completion_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p if top_p is not None else openai.Omit(),
            n=n,
        )
        return [choice.message.content for choice in completion.choices]


class AnthropicProvider(Provider):
    def __init__(self, model_name: str, api_key: str, url: str):
        super().__init__()
        self.model_name = model_name
        self.client = anthropic.Anthropic(api_key=api_key, base_url=url)

    def generate(self, messages, max_tokens: int, temperature: float, n: int,
                 top_p: float | None):
        outputs = []
        for _ in range(n):
            completion = self.client.messages.create(
                model=self.model_name,
                stream=False,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                # top_p=top_p if top_p is not None else anthropic.Omit()    # `temperature` and `top_p` cannot both be specified
            )
            outputs.append(completion.content[0].text)
        return outputs
