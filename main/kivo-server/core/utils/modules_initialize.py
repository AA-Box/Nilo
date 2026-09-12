from typing import Dict, Any
from config.logger import setup_logging
from core.utils import tts, llm, intent, memory, vad, asr
from core.utils.cache.manager import cache_manager
from core.utils.cache.config import CacheType


TAG = __name__
logger = setup_logging()


def initialize_modules(
    logger,
    config: Dict[str, Any],
    init_vad=False,
    init_asr=False,
    init_llm=False,
    init_tts=False,
    init_memory=False,
    init_intent=False,
) -> Dict[str, Any]:
    """
    Initialize all module components

    Args:
        config: config dictionary

    Returns:
        Dict[str, Any]: dictionary of all initialized modules
    """
    modules = {}

    # Initialize the TTS module
    if init_tts:
        select_tts_module = config["selected_module"]["TTS"]
        cached_config=cache_manager.get(CacheType.CONFIG,key=f"config:{select_tts_module}",namespace="TTS")
        if cached_config==config["TTS"][select_tts_module]:
            modules["tts"] = cache_manager.get(CacheType.CONFIG,key=f"module:{select_tts_module}",namespace="TTS")
        else:
            modules["tts"] = initialize_tts(config)
            cache_manager.set(CacheType.CONFIG,key=f"config:{select_tts_module}",value=config["TTS"][select_tts_module],namespace="TTS")
            cache_manager.set(CacheType.CONFIG,key=f"module:{select_tts_module}",value=modules["tts"],namespace="TTS")
        logger.bind(tag=TAG).info(f"Initialized component: tts {select_tts_module}")
        

    # Initialize the LLM module
    if init_llm:
        select_llm_module = config["selected_module"]["LLM"]
        cached_config = cache_manager.get(CacheType.CONFIG, key=f"config:{select_llm_module}", namespace="LLM")
        if cached_config == config["LLM"][select_llm_module]:
            modules["llm"] = cache_manager.get(CacheType.CONFIG, key=f"module:{select_llm_module}", namespace="LLM")
        else:
            llm_type = (
                select_llm_module
                if "type" not in config["LLM"][select_llm_module]
                else config["LLM"][select_llm_module]["type"]
            )
            modules["llm"] = llm.create_instance(
                llm_type,
                config["LLM"][select_llm_module],
            )
            cache_manager.set(CacheType.CONFIG, key=f"config:{select_llm_module}", value=config["LLM"][select_llm_module], namespace="LLM")
            cache_manager.set(CacheType.CONFIG, key=f"module:{select_llm_module}", value=modules["llm"], namespace="LLM")
        logger.bind(tag=TAG).info(f"Initialized component: llm {select_llm_module}")

    # Initialize the Intent module
    if init_intent:
        select_intent_module = config["selected_module"]["Intent"]
        cached_config = cache_manager.get(CacheType.CONFIG, key=f"config:{select_intent_module}", namespace="Intent")
        if cached_config == config["Intent"][select_intent_module]:
            modules["intent"] = cache_manager.get(CacheType.CONFIG, key=f"module:{select_intent_module}", namespace="Intent")
        else:
            intent_type = (
                select_intent_module
                if "type" not in config["Intent"][select_intent_module]
                else config["Intent"][select_intent_module]["type"]
            )
            modules["intent"] = intent.create_instance(
                intent_type,
                config["Intent"][select_intent_module],
            )
            cache_manager.set(CacheType.CONFIG, key=f"config:{select_intent_module}", value=config["Intent"][select_intent_module], namespace="Intent")
            cache_manager.set(CacheType.CONFIG, key=f"module:{select_intent_module}", value=modules["intent"], namespace="Intent")
        logger.bind(tag=TAG).info(f"Initialized component: intent {select_intent_module}")

    # Initialize the Memory module
    if init_memory:
        select_memory_module = config["selected_module"]["Memory"]
        cached_config = cache_manager.get(CacheType.CONFIG, key=f"config:{select_memory_module}", namespace="Memory")
        if cached_config == config["Memory"][select_memory_module]:
            modules["memory"] = cache_manager.get(CacheType.CONFIG, key=f"module:{select_memory_module}", namespace="Memory")
        else:
            memory_type = (
                select_memory_module
                if "type" not in config["Memory"][select_memory_module]
                else config["Memory"][select_memory_module]["type"]
            )
            modules["memory"] = memory.create_instance(
                memory_type,
                config["Memory"][select_memory_module],
                config.get("summaryMemory", None),
            )
            cache_manager.set(CacheType.CONFIG, key=f"config:{select_memory_module}", value=config["Memory"][select_memory_module], namespace="Memory")
            cache_manager.set(CacheType.CONFIG, key=f"module:{select_memory_module}", value=modules["memory"], namespace="Memory")
        logger.bind(tag=TAG).info(f"Initialized component: memory {select_memory_module}")

    # Initialize the VAD module
    if init_vad:
        select_vad_module = config["selected_module"]["VAD"]
        cached_config = cache_manager.get(CacheType.CONFIG, key=f"config:{select_vad_module}", namespace="VAD")
        if cached_config == config["VAD"][select_vad_module]:
            modules["vad"] = cache_manager.get(CacheType.CONFIG, key=f"module:{select_vad_module}", namespace="VAD")
        else:
            vad_type = (
                select_vad_module
                if "type" not in config["VAD"][select_vad_module]
                else config["VAD"][select_vad_module]["type"]
            )
            modules["vad"] = vad.create_instance(
                vad_type,
                config["VAD"][select_vad_module],
            )
            cache_manager.set(CacheType.CONFIG, key=f"config:{select_vad_module}", value=config["VAD"][select_vad_module], namespace="VAD")
            cache_manager.set(CacheType.CONFIG, key=f"module:{select_vad_module}", value=modules["vad"], namespace="VAD")
        logger.bind(tag=TAG).info(f"Initialized component: vad {select_vad_module}")

    # Initialize the ASR module
    if init_asr:
        select_asr_module = config["selected_module"]["ASR"]
        cached_config = cache_manager.get(CacheType.CONFIG, key=f"config:{select_asr_module}", namespace="ASR")
        if cached_config == config["ASR"][select_asr_module]:
            modules["asr"] = cache_manager.get(CacheType.CONFIG, key=f"module:{select_asr_module}", namespace="ASR")
        else:
            modules["asr"] = initialize_asr(config)
            cache_manager.set(CacheType.CONFIG, key=f"config:{select_asr_module}", value=config["ASR"][select_asr_module], namespace="ASR")
            cache_manager.set(CacheType.CONFIG, key=f"module:{select_asr_module}", value=modules["asr"], namespace="ASR")
        logger.bind(tag=TAG).info(f"Initialized component: asr {select_asr_module}")
    return modules


def initialize_tts(config):
    select_tts_module = config["selected_module"]["TTS"]
    tts_config = config["TTS"][select_tts_module].copy()
    tts_config.setdefault("tts_timeout", config.get("tts_timeout", 15))
    tts_type = (
        select_tts_module
        if "type" not in tts_config
        else tts_config["type"]
    )
    new_tts = tts.create_instance(
        tts_type,
        tts_config,
        str(config.get("delete_audio", True)).lower() in ("true", "1", "yes"),
    )
    return new_tts


def initialize_asr(config):
    select_asr_module = config["selected_module"]["ASR"]
    asr_type = (
        select_asr_module
        if "type" not in config["ASR"][select_asr_module]
        else config["ASR"][select_asr_module]["type"]
    )
    new_asr = asr.create_instance(
        asr_type,
        config["ASR"][select_asr_module],
        str(config.get("delete_audio", True)).lower() in ("true", "1", "yes"),
    )
    logger.bind(tag=TAG).info("ASR module initialized")
    return new_asr


def initialize_voiceprint(asr_instance, config):
    """Initialize voiceprint recognition"""
    voiceprint_config = config.get("voiceprint")
    if not voiceprint_config:
        return False  

    # Apply the config
    if not voiceprint_config.get("url") or not voiceprint_config.get("speakers"):
        logger.bind(tag=TAG).warning("Voiceprint recognition config is incomplete")
        return False
        
    try:
        asr_instance.init_voiceprint(voiceprint_config)
        logger.bind(tag=TAG).info("Voiceprint recognition enabled dynamically on the ASR module")
        logger.bind(tag=TAG).info(f"Configured speaker count: {len(voiceprint_config['speakers'])}")
        return True
    except Exception as e:
        logger.bind(tag=TAG).error(f"Failed to initialize voiceprint recognition dynamically: {str(e)}")
        return False
