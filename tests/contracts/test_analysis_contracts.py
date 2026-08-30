from codemigrator.analysis import InMemorySnapshotSource, SourceAnalysisDescriptor


def test_analysis_package_exposes_only_read_oriented_descriptor_surface() -> None:
    assert InMemorySnapshotSource.__name__ == "InMemorySnapshotSource"
    assert SourceAnalysisDescriptor.__name__ == "SourceAnalysisDescriptor"
