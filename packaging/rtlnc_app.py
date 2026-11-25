"""PyInstaller entry script for rtlnc.exe."""
import multiprocessing

from rtlnc.winapp import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
