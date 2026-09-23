# Validation status

Recorded during implementation on 2026-09-09. All exchange-order tests use an in-memory SDK double; **no real orders or funded-account checks were performed**.

Final local result: **42 tests passed**, including the opt-in full-connectome test. An offline fixture run also resumed from its saved ledger/checkpoint with balances and neural time preserved. Four upstream AgentKit/Pydantic deprecation warnings remain; they did not fail the tests.

| Check | Observed result | What it does not establish |
| --- | --- | --- |
| Rebuild from checksum-verified released files | 166,700 neurons, 25,582,938 connections and 124,177,617 contacts; every compiled array matches its lock | Completeness or physiological accuracy of the reconstruction |
| Execution/unit tests | Budget, stale data, spread, inventory, permissions, preview fees, STOP, cooldown, duplicate settlement, unknown submission and read-only public-client isolation pass | Successful execution against a particular live account, region or fee tier |
| Full-network sensory/feedback test | White RGB input activates KCs; reward and aversive pulses cause spikes in identified DAN cells; eligible synapses change | Accurate fly retinal responses or an acquired trading association |
| Same-checkpoint controls | Reward changes weights differently from no external reward; frozen memory stays exactly unchanged; checkpoint restoration works | Improved decisions, retention of a useful strategy, or conditioning equivalent to a biological experiment |
| Six accelerated observations of actual Coinbase public BTC-USDC data | One neural BUY filled in paper mode; five subsequent BUY proposals vetoed by cooldown | A realistic trading return or unbiased strategy |

In that six-observation run, the first paper fill used 9.754680320 USDC plus 0.058528081920 USDC in modeled fees. The next observation scheduled a **200 ms aversive pulse**, with **55 spikes across the two PPL101 cells**. KCs produced 11–16 spikes per observation; five candidate edges differed from baseline. Those five edges had already changed before the external loss pulse: endogenous dopamine activity can also drive the rule. Do not attribute every weight change to P&L.

The repeated BUY proposals are an important limitation. A fixed directional decoder can turn circuit bias into one-sided exposure. This run demonstrated neither successful learning nor a profitable policy. White-field neural tests activate substantially more KCs than the actual chart, so visual representation remains a major open modeling question.

The former dark chart produced no KC spikes in an early three-step probe. A light-background display restored some activity without altering the neural parameters. That is a disclosed sensory-adapter change, not evidence that we found biologically correct vision.

## Spontaneous olfactory ignition

Recorded 2026-09-23 on an Apple Silicon Mac. With the fixture chart and memory frozen, KCs fired 16, 14 and 12 spikes in the first three observations (1.5 s of neural time), consistent with the run above. At the fourth observation they fired **3,569**, then 4,562 and 4,470; total spikes per observation rose from about 390,000 to 608,000. With learning enabled and no external pulses the fourth observation reached 3,595 KC spikes, so the memory rule does not cause the jump. A paper run on public Binance prices jumped at the second observation (1,509 KC spikes).

The extra spikes come from the olfactory pathway, although the model receives no odor input: antennal-lobe local neurons (lLN1_bc and several lLN2 types), receptor neurons (ORN_VA6, ORN_DM1, HRN_VP1d) and projection neurons, which drive KCg-m and KCab-m cells. About 8,400 cells silent in the third observation spiked in the fifth. The circuit mechanism inside the model has not been isolated.

This changes how later observations read:

- KC activity then mostly reflects this self-sustained olfactory state, not the displayed chart. The 11–16 KC spikes per observation above describe pre-ignition observations only.
- Without any external pulse, PPL101 cells fired 5–27 spikes per observation before ignition and 52–79 after it. The engineered aversive pulse is added to an already active cell.
- With learning enabled and no P&L pulses, 2,735–3,091 candidate edges differed from baseline after ignition, and minimum efficacy fell to 0.47 by the sixth observation. Weight changes after ignition cannot be attributed to P&L.

No implementation error explaining it was found, and it is unchanged with memory frozen; treat it as a property of the current model. Suppressing it, for example by changing sign assumptions or adding calibrated background activity, is a modeling decision that would affect every result in this report.

```sh
python -m stonkfly run --fixture --fast --frozen --steps 6 --out runs/ignition
```

Read `KC_spikes` in `runs/ignition/events.jsonl`. Paper fills there schedule reward and aversive pulses, but frozen memory stops them from changing weights; the KC counts matched a pulse-free probe exactly.

## Reproduce

```sh
python -m pytest -q
OPENBLAS_NUM_THREADS=1 STONKFLY_FULL_TEST=1 python -m pytest -q
python -m stonkfly verify
python -m stonkfly run --fixture --fast --steps 6 --out runs/check-fixture
python -m stonkfly run --fast --steps 6 --out runs/check-public
```

The last command reads current public prices and simulates fills. Prices, signals and paper outcomes will differ. All runtime evidence stays local in `runs/`; it is not uploaded with this report. A normal `run` omits `--fast` and samples at the configured wall interval.

Before claiming learned performance, implement the held-out replay, shuffled reinforcement, exposure baselines, retention and memory-reset comparisons described in [the model](model.md). This repository currently provides a functioning experimental loop, not that empirical result.
