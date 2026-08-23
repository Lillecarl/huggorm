# Store mutex init data race

Review finding 11. store.cpp register_/lookup do check-then-assign on
void* lock_ unsynchronized; two concurrent pool adds is C++ UB and can
create two mutexes over one set.

Fix: initialize the mutex in the Store constructor or use std::once_flag.
