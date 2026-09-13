import importlib
import pkgutil

# Minimal logger to avoid depending on loguru
class SimpleLogger:
    def __init__(self, tag):
        self.tag = tag

    def info(self, msg):
        print(f"[INFO] [{self.tag}] {msg}")

    def bind(self, tag):
        return SimpleLogger(tag)

TAG = __name__
logger = SimpleLogger(TAG)

def auto_import_modules(package_name):
    """
    Auto-import all modules in the given package.

    Args:
        package_name (str): package name, e.g. 'functions'.
    """
    # Get the package path
    package = importlib.import_module(package_name)
    package_path = package.__path__

    # Iterate over all modules in the package
    for _, module_name, _ in pkgutil.iter_modules(package_path):
        # Import the module
        full_module_name = f"{package_name}.{module_name}"
        importlib.import_module(full_module_name)
        #logger.bind(tag=TAG).info(f"Module '{full_module_name}' loaded")