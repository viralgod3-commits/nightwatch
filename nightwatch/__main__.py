"""Launch with python -m nightwatch; safe for spawned analysis processes."""
if __name__ == '__main__':
    import multiprocessing
    import time
    startup_started = time.monotonic()
    multiprocessing.freeze_support()
    from .entrypoint import main
    raise SystemExit(main(startup_started=startup_started))
