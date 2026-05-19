import os
import dashscope
from dashscope.audio.asr import Recognition, RecognitionCallback
from src.utils.logger import logger

dashscope.api_key = os.getenv("DASHSCOPE_API_KEY", "")
ASR_MODEL = os.getenv("DASHSCOPE_ASR_MODEL", "paraformer-realtime-v2")


def transcribe(audio_path: str) -> str:
    logger.info(f"开始 ASR 转写：{audio_path}")

    try:
        response = Recognition(
            model=ASR_MODEL,
            format="mp3",
            sample_rate=16000,
            language_hints=["zh", "en"],
            callback=RecognitionCallback(),
        ).call(audio_path)

        if response.status_code != 200:
            logger.error(f"ASR 失败：{response.code} - {response.message}")
            return ""

        sentences = response.get_sentence()
        transcript = "".join([s["text"] for s in sentences if s.get("text")])
        logger.info(f"ASR 转写完成，共 {len(transcript)} 字")
        return transcript

    except Exception as e:
        logger.error(f"ASR 转写异常：{e}")
        return ""
