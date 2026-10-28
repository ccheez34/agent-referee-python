"""A free demo tie-break: both seats played here, in two threads. Run: py examples/demo_match.py"""
import threading

from agent_referee_client import AgentReferee

ref_a, ref_b = AgentReferee(demo=True), AgentReferee(demo=True)
match = ref_a.create(tier="weighted", seats=["alpha", "beta"], weights={"alpha": 50, "beta": 50}, title="Who writes the summary")

results = {}


def play(ref, seat):
    results[seat] = ref.join(match["match_id"], seat).play()


threads = [threading.Thread(target=play, args=(ref_a, "alpha")), threading.Thread(target=play, args=(ref_b, "beta"))]
for t in threads:
    t.start()
for t in threads:
    t.join()

for seat, st in results.items():
    print(seat, "sees winner", st["result"]["winner"], "| check ok:", st["check"]["ok"], st["check"]["problems"])
