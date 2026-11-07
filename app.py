import logging

import phonebook.config as config_module
import phonebook.errors as errors
import phonebook.server as server_module


def main() -> None:
    try:
        config = config_module.Config.from_env()
    except errors.ConfigurationError as error:
        raise SystemExit(f"Configuration error: {error}") from error

    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    LOGGER = logging.getLogger("phonebook")
    server = server_module.create_server(config)
    LOGGER.info("Serving phonebook on http://%s:%s", config.host, config.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()