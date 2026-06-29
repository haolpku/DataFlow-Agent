from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # generate
    from .generate.agent_explore_generator import AgentExploreGenerator
    from .generate.agent_explore_tree_generator import AgentExploreTreeGenerator
    # eval
    from .eval.trajectory_quality_evaluator import TrajectoryQualityEvaluator
    # filter
    from .filter.trajectory_filter import TrajectoryFilter
else:
    import sys
    from dataflow.utils.registry import LazyLoader, generate_import_structure_from_type_checking

    cur_path = "dataflow/operators/agentic_explore/"

    _import_structure = generate_import_structure_from_type_checking(__file__, cur_path)
    sys.modules[__name__] = LazyLoader(__name__, "dataflow/operators/agentic_explore/", _import_structure)
