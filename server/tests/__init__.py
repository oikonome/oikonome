# Every documented door's JSON reply the suite receives is validated
# against its documented shape (tests/api_conformance.py).
from . import api_conformance as _api_conformance

_api_conformance.install()

# Every statement the suite runs is read for the household locks it takes;
# a test that takes them out of the documented order fails
# (tests/lock_order_hook.py).
from . import lock_order_hook as _lock_order_hook

_lock_order_hook.install()

# Every test joins the threads the product started during it, so none is
# still holding a pooled connection when the next test begins
# (tests/thread_hygiene.py).
from . import thread_hygiene as _thread_hygiene

_thread_hygiene.install()
