"""Question 1: who works with whom, built only from who addresses whom.

An edge A -> B exists when a message by A lists B in `to`. Every edge carries the ids of
the messages it rests on. A message with an empty `to` is a broadcast: it is counted for
its sender and never turned into edges (guessing who a broadcast was for is inventing data).
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import Transcript


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    events: tuple[str, ...]  # message ids, transcript order


@dataclass(frozen=True)
class Graph:
    agents: tuple[str, ...]                      # every agent that produced an event, first-seen order
    edges: tuple[Edge, ...]                      # sorted by (src, dst)
    broadcasts: dict[str, tuple[str, ...]]       # sender -> message ids with an empty `to`
    only_addressed: tuple[str, ...]              # named in some `to` but produced no event


def build_graph(tr: Transcript) -> Graph:
    agents: list[str] = []
    ids: dict[tuple[str, str], list[str]] = {}
    broadcasts: dict[str, list[str]] = {}
    named: list[str] = []
    for e in tr.events:
        if e.agent not in agents:
            agents.append(e.agent)
        if e.kind != "message":
            continue
        if not e.to:
            broadcasts.setdefault(e.agent, []).append(e.id)
            continue
        for dst in dict.fromkeys(e.to):  # a name listed twice in one message is one edge use
            ids.setdefault((e.agent, dst), []).append(e.id)
            if dst not in named:
                named.append(dst)
    edges = tuple(Edge(s, d, tuple(v)) for (s, d), v in sorted(ids.items()))
    return Graph(tuple(agents), edges, {k: tuple(v) for k, v in broadcasts.items()},
                 tuple(n for n in named if n not in agents))


def pair_kind(g: Graph, e: Edge) -> str:
    if e.src == e.dst:
        return "self"
    back = any(x.src == e.dst and x.dst == e.src for x in g.edges)
    return "mutual" if back else "one-way"


def graph_to_json(g: Graph) -> dict:
    return {"agents": list(g.agents),
            "edges": [{"from": e.src, "to": e.dst, "messages": len(e.events), "events": list(e.events),
                       "kind": pair_kind(g, e)} for e in g.edges],
            "broadcasts": {k: list(v) for k, v in g.broadcasts.items()},
            "only_addressed": list(g.only_addressed)}


def render_graph(g: Graph, shown: int = 3) -> list[str]:
    out = ["## Who addresses whom",
           f"{len(g.agents)} agents · {len(g.edges)} directed edges · "
           f"{sum(len(v) for v in g.broadcasts.values())} broadcast messages (not turned into edges)", ""]
    if not g.edges:
        out.append("(no addressed messages)")
    for e in g.edges:
        cites = ", ".join(f"`{i}`" for i in e.events[:shown])
        more = f" … first {shown} of {len(e.events)}; all in --json" if len(e.events) > shown else ""
        out.append(f"- {e.src} → {e.dst} · {len(e.events)} message(s) · {pair_kind(g, e)} — {cites}{more}")
    if g.broadcasts:
        out += ["", "Broadcasts (no addressee in the source):"]
        out += [f"- {a} · {len(v)} — " + ", ".join(f"`{i}`" for i in v[:shown]) for a, v in g.broadcasts.items()]
    silent = [a for a in g.agents if not any(e.src == a or e.dst == a for e in g.edges)]
    if silent:
        out += ["", "Agents with no addressed message in or out: " + ", ".join(silent)]
    if g.only_addressed:
        out += ["", "Addressed but never appear as an agent: " + ", ".join(g.only_addressed)]
    return out
