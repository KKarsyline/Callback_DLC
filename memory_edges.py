class MemoryEdgeStore:
    """Host port. Each call reads current, authorized, explicit event relations."""

    def __init__(self, config):
        self.read_edges = config.get("list_edges", tuple)

    def list_edges(self):
        return list(self.read_edges())
