"""Run Friday's Facebook Page webhook adapter."""

import logging

from agent_bot.config import load_settings
from agent_bot.facebook_agent import FacebookAgentServer


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings()
    logging.info("Starting Friday Facebook Page adapter on %s:%s", settings.facebook_host, settings.facebook_port)
    FacebookAgentServer(settings).serve()
