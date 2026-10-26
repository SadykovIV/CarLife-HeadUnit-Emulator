def dispose_qt_object(obj):
    """Destroy a closed fixture while the shared QApplication still exists.

    QWidget.close() only hides a window by default. Its native children and
    Python signal callbacks must not survive until interpreter finalization.
    """
    from PySide6.QtCore import QCoreApplication, QEvent
    import shiboken6

    obj.deleteLater()
    QCoreApplication.sendPostedEvents(obj, QEvent.DeferredDelete)
    if shiboken6.isValid(obj):
        raise AssertionError("Deferred deletion did not destroy the Qt fixture")
