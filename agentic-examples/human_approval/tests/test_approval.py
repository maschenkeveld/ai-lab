"""The approval gate, offline: a scripted model, no network. The booking tool must run exactly when a human approves."""
from main import run_with_approvals
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel


def scripted(*steps):
    """A model that plays `steps` in order: each is a ToolCallPart name+args, or a final text."""
    queue = list(steps)

    def respond(messages, info):
        step = queue.pop(0)
        if isinstance(step, str):
            return ModelResponse(parts=[TextPart(step)])
        name, args = step
        return ModelResponse(parts=[ToolCallPart(name, args)])
    return FunctionModel(respond)


BOOK = ("book_flight", {"flight_number": "KA-AMSBRU"})


def test_an_approved_booking_runs_exactly_once():
    result, ledger = run_with_approvals("book it", lambda call: True, model=scripted(BOOK, "Booked."))
    assert [b["flight_number"] for b in ledger.booked] == ["KA-AMSBRU"]
    assert result.output == "Booked."


def test_a_denied_booking_never_runs_and_the_model_is_told_why():
    seen = []

    def respond_after_denial(messages, info):
        seen.append(str(messages[-1].parts[-1].content))
        return ModelResponse(parts=[TextPart("Understood, I did not book it.")])

    queue = [BOOK]

    def respond(messages, info):
        return ModelResponse(parts=[ToolCallPart(*queue.pop(0))]) if queue else respond_after_denial(messages, info)

    result, ledger = run_with_approvals("book it", lambda call: "too expensive", model=FunctionModel(respond))
    assert ledger.booked == []                                   # the tool never ran
    assert "too expensive" in seen[0]                            # and the model saw the reason
    assert result.output == "Understood, I did not book it."


def test_free_tools_do_not_need_approval():
    result, ledger = run_with_approvals("look", lambda call: False,
                                        model=scripted(("search_flights", {"destination": "Brussels"}), "KA-AMSBRU is 29.24 EUR."))
    assert ledger.booked == [] and "29.24" in result.output


def test_two_bookings_need_two_decisions():
    decisions = []

    def decide(call):
        decisions.append(call.args_as_dict()["flight_number"])
        return True if len(decisions) == 1 else "only one trip"

    model = scripted(("book_flight", {"flight_number": "KA-AMSBRU"}), ("book_flight", {"flight_number": "KA-AMSVIE"}), "Done.")
    _, ledger = run_with_approvals("book both", decide, model=model)
    assert decisions == ["KA-AMSBRU", "KA-AMSVIE"]
    assert [b["flight_number"] for b in ledger.booked] == ["KA-AMSBRU"]
