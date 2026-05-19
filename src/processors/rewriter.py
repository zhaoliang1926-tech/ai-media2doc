import os
import json
import yaml
from dataclasses import dataclass
from abc import ABC, abstractmethod
from typing import Optional
from src.utils.logger import logger

REWRITER_PROVIDER = os.getenv("REWRITER_PROVIDER", "claude")
PROMPTS_PATH = os.path.join(os.path.dirname(__file__), "../../config/prompts.yaml")


@dataclass
class RewriteResult:
    titles: list[str]
    content: str
    hashtags: list[str]
    comments: list[str]
    category: str
    summary: str
    # 结构拆解
    hook: str = ""
    hook_analysis: str = ""
    body_analysis: str = ""
    ending_analysis: str = ""
    # 用户画像
    emotion: str = ""
    audience: str = ""
    format: str = ""


def _load_prompts() -> dict:
    with open(PROMPTS_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _build_prompt(transcript: str, style: Optional[str] = None) -> tuple[str, str]:
    prompts = _load_prompts()
    style = style or prompts.get("default_style", "口语化短视频")
    styles = prompts.get("styles", {})
    system_prompt = styles.get(style, {}).get("system_prompt", "你是一位短视频运营专家。")
    user_prompt = prompts["rewrite_prompt_template"].format(transcript=transcript)
    return system_prompt, user_prompt


def _fix_json(s: str) -> str:
    """修复 JSON 字符串中的常见问题：字符串内的裸换行符。"""
    result = []
    in_string = False
    escape = False
    for ch in s:
        if escape:
            result.append(ch)
            escape = False
        elif ch == '\\':
            result.append(ch)
            escape = True
        elif ch == '"':
            in_string = not in_string
            result.append(ch)
        elif in_string and ch == '\n':
            result.append('\\n')
        elif in_string and ch == '\r':
            result.append('\\r')
        else:
            result.append(ch)
    return ''.join(result)


def _parse_result(raw: str) -> RewriteResult:
    try:
        clean = raw.strip()
        # 提取 JSON 块
        if "```json" in clean:
            clean = clean.split("```json")[1].split("```")[0]
        elif "```" in clean:
            clean = clean.split("```")[1].split("```")[0]
        else:
            # 兜底：找最外层 { } 提取 JSON
            start = clean.find('{')
            end = clean.rfind('}')
            if start != -1 and end != -1 and end > start:
                clean = clean[start:end + 1]
        clean = _fix_json(clean.strip())
        try:
            data = json.loads(clean)
        except json.JSONDecodeError:
            from json_repair import repair_json
            data = json.loads(repair_json(clean))
        structure = data.get("structure", {})
        portrait = data.get("portrait", {})
        return RewriteResult(
            titles=data.get("titles", []),
            content=data.get("content", ""),
            hashtags=data.get("hashtags", []),
            comments=data.get("comments", []),
            category=data.get("category", "其他"),
            summary=data.get("summary", ""),
            hook=structure.get("hook", ""),
            hook_analysis=structure.get("hook_analysis", ""),
            body_analysis=structure.get("body", ""),
            ending_analysis=structure.get("ending", ""),
            emotion=portrait.get("emotion", ""),
            audience=portrait.get("audience", ""),
            format=portrait.get("format", ""),
        )
    except Exception as e:
        logger.error(f"解析改写结果失败：{e}\n原始内容：{raw}")
        return RewriteResult([], raw, [], [], "其他", raw[:50])


class BaseRewriter(ABC):
    @abstractmethod
    def rewrite(self, transcript: str, style: Optional[str] = None) -> RewriteResult:
        pass


class ClaudeRewriter(BaseRewriter):
    def __init__(self):
        import anthropic
        self.client = anthropic.Anthropic(api_key=os.getenv("CLAUDE_API_KEY"))
        self.model = os.getenv("CLAUDE_MODEL", "claude-opus-4-5")

    def rewrite(self, transcript: str, style: Optional[str] = None) -> RewriteResult:
        system_prompt, user_prompt = _build_prompt(transcript, style)
        logger.info(f"使用 Claude 改写，模型：{self.model}")
        message = self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = message.content[0].text
        return _parse_result(raw)


class QwenRewriter(BaseRewriter):
    def __init__(self):
        import dashscope
        dashscope.api_key = os.getenv("DASHSCOPE_API_KEY")
        self.model = os.getenv("QWEN_MODEL", "qwen-max")

    def rewrite(self, transcript: str, style: Optional[str] = None) -> RewriteResult:
        from dashscope import Generation
        system_prompt, user_prompt = _build_prompt(transcript, style)
        logger.info(f"使用 Qwen 改写，模型：{self.model}")
        response = Generation.call(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            result_format="message",
        )
        raw = response.output.choices[0].message.content
        return _parse_result(raw)


class OpenAIRewriter(BaseRewriter):
    def __init__(self):
        from openai import OpenAI
        self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.model = os.getenv("OPENAI_MODEL", "gpt-4o")

    def rewrite(self, transcript: str, style: Optional[str] = None) -> RewriteResult:
        system_prompt, user_prompt = _build_prompt(transcript, style)
        logger.info(f"使用 OpenAI 改写，模型：{self.model}")
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        raw = response.choices[0].message.content
        return _parse_result(raw)


class ClaudeCliRewriter(BaseRewriter):
    """通过 claude CLI 调用，使用 Pro 账号，无需 API Key。"""

    @staticmethod
    def _strip_ansi(text: str) -> str:
        import re
        return re.sub(r'\x1b\[[0-9;]*[mGKHF]|\x1b\].*?\x07|\r', '', text)

    def rewrite(self, transcript: str, style: Optional[str] = None) -> RewriteResult:
        import subprocess
        system_prompt, user_prompt = _build_prompt(transcript, style)
        full_prompt = f"{system_prompt}\n\n{user_prompt}"
        logger.info("使用 Claude CLI 改写...")
        env = {k: v for k, v in __import__("os").environ.items() if k != "CLAUDECODE"}
        env["NO_COLOR"] = "1"
        result = subprocess.run(
            ["claude", "--output-format", "text", "-p", full_prompt],
            capture_output=True, text=True, timeout=180, env=env,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Claude CLI 调用失败：{result.stderr[:200]}")
        clean = self._strip_ansi(result.stdout)
        return _parse_result(clean)


class RewriterFactory:
    @classmethod
    def get(cls, provider: Optional[str] = None) -> BaseRewriter:
        provider = provider or REWRITER_PROVIDER
        if provider == "claude":
            return ClaudeRewriter()
        elif provider == "claude-cli":
            return ClaudeCliRewriter()
        elif provider == "qwen":
            return QwenRewriter()
        elif provider == "openai":
            return OpenAIRewriter()
        else:
            logger.warning(f"未知 provider：{provider}，使用 Qwen")
            return QwenRewriter()


def rewrite(transcript: str, style: Optional[str] = None) -> RewriteResult:
    rewriter = RewriterFactory.get()
    return rewriter.rewrite(transcript, style)


def rewrite_with_claude_cli(transcript: str, style: Optional[str] = None) -> RewriteResult:
    return ClaudeCliRewriter().rewrite(transcript, style)
