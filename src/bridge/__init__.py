"""bridge — directory-based agent environment.

Each workspace directory can carry a `.bridge/profile.yaml`. Profiles are
resolved from a self-declared root down to the launch directory, giving every
agent (and its sub-agents) inherited settings, tools and memory, while all
activity is reported through NATS to a central collector.
"""

__version__ = "0.1.0"
