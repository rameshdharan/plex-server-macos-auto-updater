.PHONY: help test check status update dry-run install uninstall rollback

help:
	@echo "Plex Media Server macOS Auto-Updater"
	@echo "------------------------------------"
	@echo "Available commands:"
	@echo "  make test        - Run automated unit test suite"
	@echo "  make check       - Check for available updates without installing"
	@echo "  make status      - Display current server and LaunchAgent service status"
	@echo "  make dry-run     - Simulate update workflow without modifying anything"
	@echo "  make update      - Manually check and apply update immediately"
	@echo "  make rollback    - Roll back to the previous .app version"
	@echo "  make install     - Install and start background LaunchAgent service"
	@echo "  make uninstall   - Stop and remove background LaunchAgent service"

test:
	python3 -m unittest discover tests

check:
	./plex_updater.py check

status:
	./plex_updater.py status

dry-run:
	./plex_updater.py update --dry-run

update:
	./plex_updater.py update

rollback:
	./plex_updater.py rollback

install:
	./install.sh

uninstall:
	./uninstall.sh
