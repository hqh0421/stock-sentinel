from typing import List, Dict, Optional, Any
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.output_parsers import StrOutputParser

from .schemas import ModelConfig


def apply_proxy(cfg: ModelConfig):
    # User requirement: all interfaces must bypass local proxies.
    import os
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        os.environ.pop(key, None)


# 规范化 Base URL，去除末尾斜杠和多余的 /chat/completions
def normalize_base_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return url
    u = url.strip().rstrip('/')
    lower = u.lower()
    if lower.endswith('/chat/completions'):
        u = u[: -len('/chat/completions')].rstrip('/')
    return u


def get_llm(cfg: ModelConfig):
    # 先应用代理环境
    apply_proxy(cfg)
    provider = cfg.provider.lower()
    if provider == "openai":
        try:
            from langchain_openai import ChatOpenAI
        except ImportError:
            raise ImportError("langchain-openai 未安装: pip install langchain-openai")
        # 官方 OpenAI 默认使用其官方根地址，不强制 base_url
        base_url = normalize_base_url(cfg.base_url) if cfg.base_url else None
        return ChatOpenAI(api_key=cfg.api_key, model=cfg.model, temperature=cfg.temperature, base_url=base_url)
    elif provider == "openai_compat":
        try:
            from langchain_openai import ChatOpenAI
        except ImportError:
            raise ImportError("langchain-openai 未安装: pip install langchain-openai")
        if not cfg.base_url:
            raise ValueError("OpenAI兼容(自定义) 需要提供 base_url（根地址，例如 https://example.com/v1，不要包含 /chat/completions）")
        base_url = normalize_base_url(cfg.base_url)
        if not base_url:
            raise ValueError("OpenAI兼容(自定义) base_url 无效，请填写根地址，例如 https://example.com/v1")
        return ChatOpenAI(api_key=cfg.api_key, model=cfg.model, temperature=cfg.temperature, base_url=base_url)
    elif provider == "anthropic":
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError:
            raise ImportError("langchain-anthropic 未安装: pip install langchain-anthropic")
        return ChatAnthropic(api_key=cfg.api_key, model=cfg.model, temperature=cfg.temperature)
    elif provider == "gemini":
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError:
            raise ImportError("langchain-google-genai 未安装: pip install langchain-google-genai")
        return ChatGoogleGenerativeAI(api_key=cfg.api_key, model=cfg.model, temperature=cfg.temperature)
    elif provider == "deepseek":
        try:
            from langchain_openai import ChatOpenAI
        except ImportError:
            raise ImportError("langchain-openai 未安装: pip install langchain-openai")
        base_url = normalize_base_url(cfg.base_url) if cfg.base_url else "https://api.deepseek.com/v1"
        return ChatOpenAI(api_key=cfg.api_key, model=cfg.model, temperature=cfg.temperature, base_url=base_url)
    elif provider == "modelscope":
        try:
            from langchain_openai import ChatOpenAI
        except ImportError:
            raise ImportError("langchain-openai 未安装: pip install langchain-openai")
        base_url = normalize_base_url(cfg.base_url) if cfg.base_url else "https://api-inference.modelscope.cn/v1"
        return ChatOpenAI(api_key=cfg.api_key, model=cfg.model, temperature=cfg.temperature, base_url=base_url)
    else:
        raise ValueError(f"不支持的提供商: {provider}")


def build_history_messages(history: List[Dict[str, str]]) -> List:
    messages = []
    for item in history:
        role = item.get("role", "user")
        content = item.get("content", "")
        if not content:
            continue
        if role == "user":
            messages.append(HumanMessage(content=content))
        elif role == "assistant":
            messages.append(AIMessage(content=content))
    return messages


def build_chain(llm, system_prompt: Optional[str], history: List[Dict[str, str]], output_parser: Optional[Any] = None):
    def _escape_braces(text: str) -> str:
        return text.replace("{", "{{").replace("}", "}}")

    guard = (
        "请严格遵守：\n"
        "- 直接回答用户问题，不要自我介绍或声明身份、模型名称、厂商、知识截止日期、上下文长度、价格等元信息。\n"
        "- 不输出营销性开场白或服务条款说明。\n"
        "- 非用户明确要求，不使用过多表情或无关客套。\n"
        "- 回答完整、结构化，必要时分点阐述。\n"
        "- **绝对禁止在回答内容中包含 URL 链接或创建“参考来源”、“引用列表”等章节**。请仅在文中适当位置使用 [1]、[2] 等角标进行引用标注，具体的链接列表由前端界面单独展示，**不要在回答末尾重复列出**。"
    )
    base_prompt = system_prompt or "你是一个有帮助的AI助手。"
    system_msg = _escape_braces(base_prompt + "\n" + guard)
    history_msgs = build_history_messages(history)
    prompt = ChatPromptTemplate.from_messages([
        ("system", system_msg),
        MessagesPlaceholder(variable_name="history"),
        ("human", "{question}"),
    ]).partial(history=history_msgs)
    parser = output_parser or StrOutputParser()
    chain = prompt | llm | parser
    return chain


def get_vision_llm(cfg: ModelConfig):
    """
    获取专门用于视觉分析的 LLM 实例
    """
    # 构造一个临时的配置对象，使用 vision_ 前缀的配置覆盖主配置
    vision_cfg = cfg.model_copy()
    vision_cfg.provider = cfg.vision_provider or cfg.provider
    vision_cfg.model = cfg.vision_model or "qwen3-vl-flash"
    vision_cfg.api_key = cfg.vision_api_key or cfg.api_key
    vision_cfg.base_url = cfg.vision_base_url or cfg.base_url
    
    # 复用 get_llm 逻辑
    return get_llm(vision_cfg)


def analyze_image(cfg: ModelConfig, image_base64: str, prompt_text: str = "请分析这张图片的内容"):
    """
    专门用于图片分析的函数，构建多模态消息并调用 LLM。
    使用配置中的 vision_model 进行分析。
    """
    llm = get_vision_llm(cfg)
    
    # 构造多模态消息
    # 注意：不同提供商/LangChain版本对多模态的封装略有差异，
    # 但 ChatOpenAI / ChatAnthropic / ChatGoogleGenerativeAI
    # 通用做法是 HumanMessage content 为 list
    
    # 简单的 mime type 猜测，这里统一假设为 png (Playwright 截图默认 png)
    image_data = f"data:image/png;base64,{image_base64}"
    
    message = HumanMessage(
        content=[
            {"type": "text", "text": prompt_text},
            {
                "type": "image_url",
                "image_url": {"url": image_data}
            }
        ]
    )
    
    # 直接 invoke
    try:
        response = llm.invoke([message])
        return response.content if hasattr(response, "content") else str(response)
    except Exception as e:
        return f"图片分析失败: {str(e)}"
