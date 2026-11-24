"""Toga app that runs the u3dconv web server and shows it in a WebView.

How it fits together
--------------------
1. startup() starts u3dconv.web.start_server() on a free loopback port. The
   server runs in a background thread inside this process.
2. A toga.WebView loads the server's URL (which carries the access token).
3. A WKWebView cannot save <a download> links, so the web page asks the server
   to "share" finished files. The server calls _share_file() below, which opens
   the iOS share sheet (Save to Files, AirDrop, ...) for that file.

Files live under the app's cache folder, which iOS may clear when the app is not
running. Everything the user wants to keep should be exported through the
share sheet.

This module needs Toga and rubicon-objc, so it only runs on a device or the iOS
simulator. It is not exercised by the unit tests.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import toga
from toga.style import Pack

from u3dconv.web import start_server


class U3DConvApp(toga.App):
    def startup(self):
        # A fresh workspace on every launch: nothing from a previous run is reused.
        self.workspace = Path(self.paths.cache) / "u3dconv"
        shutil.rmtree(self.workspace, ignore_errors=True)
        self.workspace.mkdir(parents=True, exist_ok=True)

        # port=0 lets the OS choose a free port. The server only listens on
        # 127.0.0.1, so nothing else on the network can reach it.
        self.server = start_server(
            host="127.0.0.1",
            port=0,
            workspace=str(self.workspace),
            export_handler=self._share_file,
        )

        self.web_view = toga.WebView(url=self.server.url, style=Pack(flex=1))
        self.main_window = toga.MainWindow(title=self.formal_name)
        self.main_window.content = self.web_view
        self.main_window.show()

    # -- native share sheet --------------------------------------------------------

    def _share_file(self, path: str, name: str) -> None:
        """Called on a server thread; UIKit work is handed to the main thread."""
        self.loop.call_soon_threadsafe(self._present_share_sheet, path)

    def _present_share_sheet(self, path: str) -> None:
        from rubicon.objc import ObjCClass

        NSURL = ObjCClass("NSURL")
        UIActivityViewController = ObjCClass("UIActivityViewController")

        window = self.main_window._impl.native  # the UIWindow
        root = window.rootViewController
        # If another sheet is already showing, present from the top-most controller.
        while root.presentedViewController is not None:
            root = root.presentedViewController

        file_url = NSURL.fileURLWithPath(path)
        sheet = UIActivityViewController.alloc().initWithActivityItems(
            [file_url], applicationActivities=None
        )

        # On iPad the sheet is a popover and needs an anchor view or it will crash.
        popover = sheet.popoverPresentationController
        if popover is not None:
            popover.sourceView = root.view

        root.presentViewController(sheet, animated=True, completion=None)


def main() -> toga.App:
    return U3DConvApp(
        formal_name="u3dconv",
        app_id="com.example.u3dconv-ios",
    )
