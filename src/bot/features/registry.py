"""Central feature registry and management (simplified)."""

from typing import Any, Dict, Optional

import structlog

from src.config.settings import Settings
from src.storage.facade import Storage

from .file_handler import FileHandler
from .image_handler import ImageHandler
from .voice_handler import VoiceHandler

logger = structlog.get_logger(__name__)


class FeatureRegistry:
    """Manage all bot features."""

    def __init__(self, config: Settings, storage: Storage = None, security: Any = None):
        self.config = config
        self.storage = storage
        self.security = security
        self.features: Dict[str, Any] = {}
        self._initialize_features()

    def _initialize_features(self):
        """Initialize enabled features."""
        logger.info("Initializing bot features")

        if self.config.enable_file_uploads and self.security:
            try:
                self.features["file_handler"] = FileHandler(
                    config=self.config, security=self.security
                )
                logger.info("File handler feature enabled")
            except Exception as e:
                logger.error("Failed to initialize file handler", error=str(e))

        try:
            self.features["image_handler"] = ImageHandler(config=self.config)
            logger.info("Image handler feature enabled")
        except Exception as e:
            logger.error("Failed to initialize image handler", error=str(e))

        voice_key_available = (
            (self.config.voice_provider == "local")
            or (self.config.voice_provider == "openai" and self.config.openai_api_key)
            or (self.config.voice_provider == "mistral" and self.config.mistral_api_key)
        )
        if self.config.enable_voice_messages and voice_key_available:
            try:
                self.features["voice_handler"] = VoiceHandler(config=self.config)
                logger.info("Voice handler feature enabled")
            except Exception as e:
                logger.error("Failed to initialize voice handler", error=str(e))

        logger.info(
            "Feature initialization complete",
            enabled_features=list(self.features.keys()),
        )

    def get_feature(self, name: str) -> Optional[Any]:
        return self.features.get(name)

    def is_enabled(self, feature_name: str) -> bool:
        return feature_name in self.features

    def get_file_handler(self) -> Optional[FileHandler]:
        return self.get_feature("file_handler")

    def get_image_handler(self) -> Optional[ImageHandler]:
        return self.get_feature("image_handler")

    def get_voice_handler(self) -> Optional[VoiceHandler]:
        return self.get_feature("voice_handler")

    # Stubs for removed features (handlers may call these)
    def get_git_integration(self):
        return None

    def get_quick_actions(self):
        return None

    def get_session_export(self):
        return None

    def get_conversation_enhancer(self):
        return None

    def get_enabled_features(self) -> Dict[str, Any]:
        return self.features.copy()

    def shutdown(self):
        self.features.clear()
        logger.info("Feature shutdown complete")
