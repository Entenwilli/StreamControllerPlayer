from src.backend.PluginManager.ActionBase import ActionBase

from ..common import get_status, send_request_async, stop_request, shorten

IDLE_BG = [0, 0, 0, 0]  # transparent
ACTIVE_BG = [0, 0, 0, 0]  # transparent
ERROR_BG = [0, 0, 0, 0]  # transparent


class StopAll(ActionBase):
    """Stops and removes every currently playing track."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._busy = False
        self._last_error = None

    def on_ready(self):
        self.set_top_label(self.plugin_base.lm.get("actions.stop-all.label"), font_size=12)
        self.update_display()

    def on_tick(self):
        self.update_display()

    def on_key_down(self):
        if self._busy:
            return

        def done(kind, payload):
            self._busy = False
            self._last_error = payload if kind == "Error" else None
            get_status(force=True)
            self.update_display()

        self._busy = True
        send_request_async(stop_request(), on_done=done)

    def update_display(self):
        if self._last_error:
            self.set_background_color(ERROR_BG)
            self.set_bottom_label(shorten(self._last_error, 20), font_size=10)
            return

        # No daemon reachable is just the resting state (nothing playing
        # yet), not an error - player-cli only starts on the first `play`.
        status = get_status() or {}
        count = len(status.get("tracks", []))
        if count:
            self.set_background_color(ACTIVE_BG)
            self.set_bottom_label(
                self.plugin_base.lm.get("actions.stop-all.state-active").format(count=count), font_size=10
            )
        else:
            self.set_background_color(IDLE_BG)
            self.set_bottom_label(self.plugin_base.lm.get("actions.stop-all.state-idle"), font_size=10)
