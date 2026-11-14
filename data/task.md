# Zenline AI – FDE take-home task

Zenline helps retailers turn product, market, competitor, and performance data into actionable workflows. Our Forward Deployed Engineers work close to customers: they understand a messy commercial question, trace the available data, build a useful product or workflow slice, and explain the result in business language.

This exercise is a two-hour simulation of that work. We are not looking for a polished production system. We are looking for clear judgment, source-backed recommendations, a small working artifact, and pragmatic tradeoffs.

## Task

Build a mini category opportunity review for a retail category.

You are given a customer brief:

> The customer wants to understand which products or subcategories need attention and what actions they should take next.
> 

Your job is to explore the provided data, identify the most useful opportunities, and create a small artifact that helps a retail stakeholder make decisions. The artifact can be a simple app, notebook, script-generated report, dashboard, or structured Markdown output.

Use this time split:

- 45 minutes: explore the data, clarify assumptions, and decide what is worth building.
- 60 minutes: build the smallest useful artifact.
- 15 minutes: clean up the output, README, and handoff notes.

AI tools are allowed. Treat them as part of your workflow, but make sure the final recommendations are traceable to the provided data.

## Data

Attached you’ll find a compact dataset that can be understood quickly:

- SKU performance data: product id, product name, brand, category, subcategory, revenue, units sold, margin, stock status, and recent sales trend.
- Competitor or market signal data: competitor product name, brand, category, price, rank or popularity signal, source, and optional trend score.
- Product metadata: price, pack size or variant, private-label flag if relevant, and any available product attributes.
- Customer context: the retail category, the customer goal, any constraints, and the business actions they care about, such as delist, promote, price review, assortment gap, or supplier follow-up.

The dataset should be small enough to inspect manually within 45 minutes. A few dozen SKUs and a few dozen competitor or market rows are enough.

## Expected Outcome

Submit a small, runnable or readable artifact plus a short README.

In the first 15 minutes of the follow-up interview, be prepared to explain your technical setup, how to run or inspect the artifact, and the main implementation choices.

The output should include:

- 3-5 evidence-backed recommendations.
- A simple artifact a retail stakeholder can use.
- A short README covering assumptions, setup, and next improvements.
- Your github/history

We will evaluate the assignment on product judgment, agentic working, evidence quality, workflow thinking, implementation pragmatism, and communication clarity.