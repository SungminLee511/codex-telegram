"""Main entry point for Codex Telegram Bot."""

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import structlog

from src import __version__
from src.bot.core import CodexBot
from src.codex import (
    CodexIntegration,
    SessionManager,
)
from src.codex.cli_integration import CodexCLIManager
from src.config.features import FeatureFlags
from src.config.settings import Settings
from src.exceptions import ConfigurationError
from src.security.audit import AuditLogger, InMemoryAuditStorage
from src.security.auth import (
    AuthenticationManager,
    InMemoryTokenStorage,
    TokenAuthProvider,
    WhitelistAuthProvider,
)
from src.security.rate_limiter import RateLimiter
from src.security.validators import SecurityValidator
from src.storage.facade import Storage
from src.storage.session_storage import SQLiteSessionStorage


def setup_logging(debug: bool = False) -> None:
    """Configure structured logging."""
    level = logging.DEBUG if debug else logging.INFO

    logging.basicConfig(
        level=level,
        format="%(message)s",
        stream=sys.stdout,
    )

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            (
                structlog.processors.JSONRenderer()
                if not debug
                else structlog.dev.ConsoleRenderer()
            ),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Codex Telegram Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--version", action="version", version=f"Codex Telegram Bot {__version__}"
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--config-file", type=Path, help="Path to configuration file")

    return parser.parse_args()


def _assert_ports_available(config: Settings) -> None:
    """Fail fast with a clear message if a needed port is already bound.

    Multi-bot: two bots with the same API/webhook port would otherwise crash
    deep in the server stack with an opaque traceback. Only ports for ENABLED
    features are checked — most bots run with both disabled, in which case
    ports are irrelevant and nothing is checked.
    """
    import socket

    logger = structlog.get_logger()
    to_check = []
    if config.enable_api_server:
        to_check.append(("api_server_port", config.api_server_port))
    if config.webhook_url:
        to_check.append(("webhook_port", config.webhook_port))

    for name, port in to_check:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError as e:
            raise ConfigurationError(
                f"Port {port} ({name}) for bot '{config.bot_id}' is already in "
                f"use ({e}). Give each bot a distinct {name} in its .env."
            ) from e
        finally:
            sock.close()
        logger.info("Port available", bot_id=config.bot_id, name=name, port=port)


async def create_application(config: Settings) -> Dict[str, Any]:
    """Create and configure the application components."""
    logger = structlog.get_logger()
    logger.info("Creating application components")

    features = FeatureFlags(config)

    # Initialize storage system
    storage = Storage(config.database_url)
    await storage.initialize()

    # Create security components
    providers = []

    if config.allowed_users:
        providers.append(WhitelistAuthProvider(config.allowed_users))

    if config.enable_token_auth:
        token_storage = InMemoryTokenStorage()
        providers.append(TokenAuthProvider(config.auth_token_secret, token_storage))

    if not providers and config.development_mode:
        logger.warning("No auth providers configured - dev allow-all mode")
        providers.append(WhitelistAuthProvider([], allow_all_dev=True))
    elif not providers:
        raise ConfigurationError("No authentication providers configured")

    auth_manager = AuthenticationManager(providers)
    security_validator = SecurityValidator(
        config.approved_directory,
        disable_security_patterns=config.disable_security_patterns,
    )
    rate_limiter = RateLimiter(config)

    audit_storage = InMemoryAuditStorage()
    audit_logger = AuditLogger(audit_storage)

    # Create Codex integration
    session_storage = SQLiteSessionStorage(storage.db_manager)
    session_manager = SessionManager(config, session_storage)

    logger.info("Using Codex CLI integration")
    sdk_manager = CodexCLIManager(config, security_validator=security_validator)

    codex_integration = CodexIntegration(
        config=config,
        sdk_manager=sdk_manager,
        session_manager=session_manager,
    )

    dependencies = {
        "auth_manager": auth_manager,
        "security_validator": security_validator,
        "rate_limiter": rate_limiter,
        "audit_logger": audit_logger,
        "codex_integration": codex_integration,
        "storage": storage,
    }

    bot = CodexBot(config, dependencies)

    logger.info("Application components created successfully")

    return {
        "bot": bot,
        "codex_integration": codex_integration,
        "storage": storage,
        "config": config,
        "features": features,
        "auth_manager": auth_manager,
        "security_validator": security_validator,
    }


async def run_application(app: Dict[str, Any]) -> None:
    """Run the application with graceful shutdown handling."""
    logger = structlog.get_logger()
    bot: CodexBot = app["bot"]
    codex_integration: CodexIntegration = app["codex_integration"]
    storage: Storage = app["storage"]

    shutdown_event = asyncio.Event()

    def signal_handler(signum: int, frame: Any) -> None:
        logger.info("Shutdown signal received", signal=signum)
        shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        logger.info("Starting Codex Telegram Bot")
        await bot.initialize()

        tasks = []
        bot_task = asyncio.create_task(bot.start())
        tasks.append(bot_task)

        shutdown_task = asyncio.create_task(shutdown_event.wait())
        tasks.append(shutdown_task)

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                logger.error("Task failed", task=task.get_name(), error=str(exc))

        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    except Exception as e:
        logger.error("Application error", error=str(e))
        raise
    finally:
        logger.info("Shutting down application")
        try:
            await bot.stop()
            await codex_integration.shutdown()
            await storage.close()
        except Exception as e:
            logger.error("Error during shutdown", error=str(e))

        logger.info("Application shutdown complete")


async def main() -> None:
    """Main application entry point."""
    args = parse_args()
    setup_logging(debug=args.debug)

    logger = structlog.get_logger()
    logger.info("Starting Codex Telegram Bot", version=__version__)

    try:
        from src.config import FeatureFlags, load_config

        config = load_config(config_file=args.config_file)
        features = FeatureFlags(config)

        # Multi-bot: fail fast on port collisions for enabled servers.
        _assert_ports_available(config)

        logger.info(
            "Configuration loaded",
            bot_id=config.bot_id,
            environment="production" if config.is_production else "development",
            enabled_features=features.get_enabled_features(),
            debug=config.debug,
        )

        app = await create_application(config)
        await run_application(app)

    except ConfigurationError as e:
        logger.error("Configuration error", error=str(e))
        sys.exit(1)
    except Exception as e:
        logger.exception("Unexpected error", error=str(e))
        sys.exit(1)


def run() -> None:
    """Synchronous entry point for setuptools."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown requested by user")
        sys.exit(0)


if __name__ == "__main__":
    run()
