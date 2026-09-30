"""Launch with python -m nightwatch; safe for spawned analysis processes."""
if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    from .entrypoint import main
    raise SystemExit(main())
