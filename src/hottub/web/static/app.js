// The only JavaScript of our own. HTMX (vendor/) does the rest; see the top
// of hottub/web/pages.py.
//
// Shows a notice when the controller itself cannot be reached (the Pi is
// off, or this device has left the home network), and hides it again when
// a request gets through. Without it the page would silently go stale.
(function () {
  "use strict";

  function showOffline(show) {
    var notice = document.getElementById("offline");
    if (notice) {
      notice.hidden = !show;
    }
  }

  document.addEventListener("htmx:sendError", function () { showOffline(true); });
  document.addEventListener("htmx:responseError", function () { showOffline(true); });
  document.addEventListener("htmx:afterRequest", function (event) {
    if (event.detail.successful) {
      showOffline(false);
    }
  });
})();
