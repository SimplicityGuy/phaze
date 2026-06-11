
## 31-05 environmental note
- tests/test_services/test_agent_task_router.py (7 tests) require a live Redis on
  localhost:6379 and use real SAQ queues (not the _queue_fakes doubles). They fail
  in this sandbox with redis.exceptions.ConnectionError because Redis is not running.
  Pre-existing + environmental; NOT caused by Plan 31-05. Out of scope — do not fix.
