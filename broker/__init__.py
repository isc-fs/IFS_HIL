"""
hil-broker — the single owner of SPI/I2C/GPIO on the HIL bench.

See docs/broker_migration_plan.md for the migration plan this package
implements. Phase 1 ships the server skeleton, the RPC surface v1, and
a fake bus for hardware-free unit tests. No client has been migrated
yet — the dashboard and pytest suite continue to bypass the broker.
"""

__version__ = "0.1.0"
