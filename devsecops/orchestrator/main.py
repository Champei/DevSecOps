
import argparse
import json

from langgraph.types import Command

from orchestrator.workflow import build_graph


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--resume", choices=["approve", "reject"])
    args = parser.parse_args()


    graph = build_graph()

    config = {
        "configurable": {
            "thread_id": args.job_id,
        }
    }

    if args.resume:
        result = graph.invoke(
            Command(resume={"approved": args.resume == "approve"}),
            config=config,
        )
    else:
        result = graph.invoke(
            {
                "job_id": args.job_id,
                "repo_url": args.repo,
                "workspace": "",
                "base_commit": "",
                "retry_count": 0,
                "max_retries": 3,
                "approved": None,
                "status": "PENDING",
                "events": [],
                "audit_report": {},
                "patch": "",
                "feedback": "",
                "verification": {},
            },
            config=config,
        )

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
