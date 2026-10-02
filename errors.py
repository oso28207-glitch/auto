"""
نظام أخطاء موحّد. أي خطأ هنا يوقف البرنامج بالكامل.
"""


class SooFatalError(Exception):
    """خطأ خطير يوقف كل العمليات فوراً."""
    def __init__(self, stage: str, message: str, original: Exception = None):
        self.stage = stage
        self.message = message
        self.original = original
        super().__init__(f"[{stage}] {message}")


class DownloadError(SooFatalError):
    def __init__(self, message, original=None):
        super().__init__("DOWNLOAD", message, original)


class UploadError(SooFatalError):
    def __init__(self, message, original=None):
        super().__init__("UPLOAD", message, original)


class SourceError(SooFatalError):
    def __init__(self, message, original=None):
        super().__init__("SOURCE", message, original)


class ConfigError(SooFatalError):
    def __init__(self, message, original=None):
        super().__init__("CONFIG", message, original)


class BuildError(SooFatalError):
    def __init__(self, message, original=None):
        super().__init__("BUILD", message, original)