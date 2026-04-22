# Compare & Contrast — Dual Implementation of an Expense Approval Workflow

**Name:** Obaida Kandakji  
**Student Number:** 041272028  
**Course Code:** CST8917 - Serverless Applications  
**Date:** April 21, 2026

## Version A Summary

Version A implements the expense approval workflow with Azure Durable Functions using the Python v2 programming model. The solution is centered in `version-a-durable-functions/function_app.py` and exposes an HTTP starter endpoint, a status endpoint, and a manager decision endpoint. Inside the orchestration, the workflow validates the request, auto-approves expenses under $100, waits for a manager decision for higher amounts, and escalates the request if no response arrives before the timeout.

The most important design choice in this version is the Durable Functions Human Interaction pattern. The orchestration uses `wait_for_external_event("ManagerDecision")` together with a durable timer, which makes the approval wait feel natural and keeps the logic readable in one place. Validation, finalization, and employee notification are modeled as activity functions. For local development, notifications were simulated with logging instead of a real email service so that the full workflow could be tested quickly with the provided `test-durable.http` file.

One of the main strengths of Version A is that the business rules are easy to follow from top to bottom. I could see the full decision path in code without jumping between multiple Azure resources. The main limitation is that, while orchestration status is available through instance IDs and status URLs, it is less visual than a Logic App run history in the Azure portal.

## Version B Summary

Version B implements the same business rules using Azure Logic Apps and Azure Service Bus. In this version, the helper Azure Function app handles validation, queue ingress, and manager decision storage. A Service Bus queue receives expense requests, and the Logic App orchestrates the rest of the workflow. After processing, the Logic App sends the final outcome to a Service Bus topic and triggers an Office 365 email notification to the employee.

The key design challenge in Version B was the manager approval step. Logic Apps does not provide the same built-in Human Interaction pattern as Durable Functions, so I used a workaround: manager decisions are posted to the helper Function app, stored in Azure Table Storage, and then polled by the Logic App every 15 seconds for up to 2 minutes. If no decision is found in time, the workflow marks the expense as escalated. The topic includes filtered subscriptions so approved, rejected, and escalated outcomes can be routed cleanly.

What worked well in Version B was the portal experience. Once deployed, the run history made it very easy to see which branch executed and where a problem happened. The trade-off was the amount of setup required. This version depends on more Azure resources and is harder to test fully end to end because the orchestration is split between the helper Function app, Service Bus, Table Storage, the Logic App itself, and the Office 365 connector.

## Comparison Analysis

### Development Experience

Version A with Durable Functions was faster to build. The main reason was that almost all of the business logic lived in one file, `version-a-durable-functions/function_app.py`. I could read the workflow in order: validate, auto-approve under $100, otherwise wait for a manager decision or a timeout, then notify. That made the system feel small and easier to reason about.

Version B took longer because the logic was split across the Logic App, a helper Function app, Service Bus, Table Storage, and the Office 365 connection. The approval step also needed a workaround: store the decision in a table, then poll for it every 15 seconds. Logic Apps was nicer to look at once it was deployed because the run history is visual, but Durable Functions gave me more confidence that the rules were actually correct.

### Testability

Durable Functions was easier to test locally. The repo already has `version-a-durable-functions/test-durable.http`, so I could start the app, send an expense, post a manager decision, and then check the orchestration status URL. That felt like I was testing the real workflow, not just a tiny helper.

Version B was harder to test end to end because the orchestration is in Azure Logic Apps, not just in local Python. The helper Function app is easy enough to test, but the full flow depends on the queue, the Logic App, the polling loop, the topic, and the email connector. The repo shows this clearly because `version-b-logic-apps/test-expense.http` points to a deployed helper URL. So both versions could have automated tests, but Durable Functions was much easier to test as a full workflow.

### Error Handling

Both versions handle failure, but in different ways. In Durable Functions, failure handling is easier to follow because it is written directly in Python. If validation fails, the workflow returns a `validation_error` result right away and still goes through notification. That made the bad path easy to understand.

Logic Apps makes failures very visible in the portal, especially when a step turns red or the workflow hits the `Terminate` action. The problem is that recovery logic feels more spread out because Version B has more moving parts: queue trigger, helper API, table reads and writes, topic send, and email connector. So I would say Logic Apps was easier to see, but Durable Functions gave more control over retries and recovery.

### Human Interaction Pattern

This was the biggest difference. Durable Functions handled "wait for manager approval" in a very natural way by using `wait_for_external_event("ManagerDecision")` and a durable timer. The workflow can pause, wait, and continue without awkward extra steps.

Version B worked, but it felt like a workaround. The Logic App had to poll the helper API every 15 seconds for up to 2 minutes and check Table Storage for a decision. That gets the right result, but it is less natural and adds extra actions and cost. For this project, Durable Functions was clearly the better fit for human interaction.

### Observability

Logic Apps was easier to monitor in the portal. The run history is visual, so I could quickly see which branch ran and where a problem happened. That makes it really good for demos and quick checks after deployment.

Durable Functions was still solid because each orchestration gives an instance ID and status URL, but it is less visual. I found Durable better when I wanted to understand the rule behind an outcome, and Logic Apps better when I wanted to see the run shape fast. So for observability, I would give the edge to Logic Apps for monitoring and Durable Functions for code-level diagnosis.

### Cost

For Version A, I assumed about 6 function executions per expense and about 3 GB-seconds per expense in total. That keeps the low-volume case inside the free grant and makes the high-volume case still pretty cheap.

For Version B, the cost grows faster because it combines Logic App built-in actions, standard connector executions, the helper Function app, and Service Bus. The polling design matters too. At low volume, the 1-minute queue trigger still creates billable checks even when the queue is mostly empty, and the Service Bus Standard namespace adds a fixed monthly charge.

| Volume | Version A: Durable Functions | Version B: Logic Apps + Service Bus |
| --- | --- | --- |
| ~100 expenses/day | about **$0.00-$1.00/month** | about **$17/month** |
| ~10,000 expenses/day | about **$8-$9/month** | about **$245-$250/month** |

The exact number will change based on run time, connector usage, and how often the manager responds before timeout. Still, the big picture is clear. Version A stays very cheap because the durable timer does not keep burning compute while it waits. Version B costs more because of the extra services, the connector calls, and the polling design.

## Recommendation

If a team asked me to build this expense workflow for production, I would choose Durable Functions.

The main reason is that the hardest part of this assignment is the human approval step, and Durable Functions fits that problem really well. The wait for manager approval, the timeout, and the final branch logic all feel natural in the Durable model. I also trust the code-first version more because the business rules are all in one place. That makes it easier to test locally, easier to review, and easier to change later without wondering which Azure resource contains the real logic. The cost is also much better, especially once the number of requests goes up.

I would choose the Logic Apps approach in a different kind of team. If the workflow was mostly connecting lots of Microsoft 365 or SaaS services, and the team wanted a visual designer plus very clear run history in the portal, Logic Apps could be the better choice. I could also see it being useful if non-developers or junior team members needed to understand the workflow quickly from the portal view.

For this specific project though, the Logic App version feels more like a workaround because the approval wait had to be simulated with polling. It works, but it is not the cleanest design. So my final recommendation is Durable Functions for production, and Logic Apps only when the bigger priority is low-code integration visibility rather than code-level control.

## References

- [Azure Pricing Calculator](https://azure.microsoft.com/pricing/calculator/)

## AI Disclosure

AI was used to fix broken code and fixed grammer and structuring of readme
