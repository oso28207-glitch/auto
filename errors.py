"""نظام الأخطاء المركزي — أي خطأ هنا يوقف كل شيء فوراً."""


class SooFatalError(Exception):
    def __init__(self, stage: str, message: str, original: Exception = None):
        self.stage = stage
        self.message = message
        self.original = original
        super().__init__(f"[{stage}] {message}")


class ConfigError(SooFatalError):
    def __init__(self, m, o=None): super().__init__("CONFIG", m, o)


class SourceError(SooFatalError):
    def __init__(self, m, o=None): super().__init__("SOURCE", m, o)


class DownloadError(SooFatalError):
    def __init__(self, m, o=None): super().__init__("DOWNLOAD", m, o)


class UploadError(SooFatalError):
    def __init__(self, m, o=None): super().__init__("UPLOAD", m, o)


class BuildError(SooFatalError):
    def __init__(self, m, o=None): super().__init__("BUILD", m, o)