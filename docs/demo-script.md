# Consumer demo script

## Preparation

1. Start the API and Streamlit UI.
2. Prepare a synthetic consumer complaint and one PDF or image evidencing a
   date, amount, protocol or supplier response.
3. Keep the OpenAPI page and Consumer retrieval dataset available.

## Five-minute walkthrough

**1. Product boundary.** Explain that Give Exit serves consumers and creates a
reviewable extrajudicial-notice draft. It does not decide who is legally right,
predict litigation or send the notice.

**2. Intake.** Enter the complaint, desired resolution, supplier, dates,
protocols and financial values. Point out that the system treats the narrative
as allegations and requires explicit confirmation.

**3. Evidence safety.** Upload the document. Show extraction/OCR metadata,
source hashes, monetary references and prompt-injection status. Explain that the
raw file is deleted and a blocked document never reaches RAG.

**4. Grounded generation.** Confirm the facts and generate the notice. Show the
canonical legal sources, evidence citations, requests and transparent settlement
scenario. Emphasize that the prose and citations are assembled deterministically.

**5. Audit.** Open the retrieval audit. Show legal/evidence query separation,
hybrid ranking, chunk IDs, source metadata, hashes and final inclusion flags.

**6. Evaluation.** Run:

```bash
python -m app.evaluation.consumer_runner
python -m app.evaluation.security_benchmark
```

Explain that the first is an engineering-authored legal retrieval seed, not a
claim of lawyer-certified quality.

## Useful questions

- **Why hybrid retrieval?** Exact statute numbers, protocols, dates and amounts
  matter alongside semantic paraphrases from consumers.
- **Where is the LLM?** Only in bounded semantic prompt-injection review. It
  does not write the notice or invent citations.
- **How are citations audited?** The backend reconstructs them from selected
  chunk IDs, page mappings, canonical source metadata and hashes.
- **What blocks production?** Durable encrypted storage, user/tenant identity,
  retention and consent controls, worker queues, PII minimization, monitoring
  and independent Brazilian legal review.
