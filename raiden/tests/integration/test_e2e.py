# -*- coding: utf-8 -*-
import pytest

from raiden.utils import sha3
from raiden.tests.utils.transfer import (
    direct_transfer,
    mediated_transfer,
    channel,
    get_sent_transfer,
)
from raiden.tests.utils.log import get_all_state_changes, get_all_state_events
from raiden.transfer.state_change import (
    Block,
    RouteState,
)
from raiden.transfer.mediated_transfer.state_change import (
    ActionInitInitiator,
    ActionInitMediator,
    ActionInitTarget,
    ReceiveSecretRequest,
    ReceiveSecretReveal
)
from raiden.transfer.events import (
    EventTransferSentSuccess,
)
from raiden.transfer.mediated_transfer.events import (
    EventUnlockSuccess,
    SendBalanceProof,
    SendMediatedTransfer,
    SendRevealSecret,
    SendSecretRequest,
)
from raiden.messages import MediatedTransfer


def mediated_transfer_almost_equal(first, second):
    assert first.identifier == second.identifier, "identifier doesn't match"
    assert first.token == second.token, "token address doesn't match"
    assert first.lock.amount == second.lock.amount, "lock amount doesn't match"
    assert first.lock.hashlock == second.lock.hashlock, "lock hashlock doesn't match"
    assert first.target == second.target, "target doesn't match"
    assert first.initiator == second.initiator, "initiator doesn't match"


def assert_path_mediated_transfer(*transfers):
    assert all(
        isinstance(t, MediatedTransfer)
        for t in transfers
    ), 'all transfers must be of type MediatedTransfer'

    for first, second in zip(transfers[:-1], transfers[1:]):
        mediated_transfer_almost_equal(first, second)

        assert first.recipient == second.sender, 'transfers are out-of-order'
        assert first.lock.expiration > second.lock.expiration, 'lock expiration is not decreasing'


def check_nested_attrs(item, data):
    for name, value in data.iteritems():
        item_value = getattr(item, name)

        if isinstance(value, dict):
            if not check_nested_attrs(item_value, value):
                return False

        elif item_value != value:
            return False

    return True


def must_contain_entry(item_list, type_, data):
    """ A node might have duplicated state changes or code changes may change
    order / quantity of the events.

    The number of state changes is undeterministic since it depends on the
    number of retries from the protocol layer.

    This is completely undeterministic since the protocol retries depends on
    timeouts and the cooperative scheduling of the running greenlets.
    Additionally the order / quantity of greenlet switchs will change as the
    code evolves.

    This utility checks the list of state changes for an entry of the correct
    type with the expected data, ignoring *new* fields, repeated entries, and
    unexpected entries.
    """
    # item_list may be composed of state changes or events
    for item in item_list:
        if isinstance(item, type_):
            if check_nested_attrs(item, data):
                return True

    return False


@pytest.mark.parametrize('privatekey_seed', ['fullnetwork:{}'])
@pytest.mark.parametrize('channels_per_node', [2])
@pytest.mark.parametrize('number_of_nodes', [4])
@pytest.mark.parametrize('settle_timeout', [50])
def test_fullnetwork(
        raiden_chain,
        token_addresses,
        deposit,
        settle_timeout,
        reveal_timeout):

    # The network has the following topology:
    #
    #   App0 <---> App1
    #    ^          ^
    #    |          |
    #    v          v
    #   App3 <---> App2

    token_address = token_addresses[0]

    app0, app1, app2, app3 = raiden_chain  # pylint: disable=unbalanced-tuple-unpacking
    channel_0_1 = channel(app0, app1, token_address)
    channel_3_2 = channel(app3, app2, token_address)
    channel_0_3 = channel(app0, app3, token_address)

    # Exhaust the channel deposit (to force the mediated transfer to go backwards)
    amount = deposit
    direct_transfer(app0, app1, token_address, amount)
    assert get_sent_transfer(channel_0_1, 0).transferred_amount == amount

    amount = int(deposit / 2.)
    mediated_transfer(
        app0,
        app2,
        token_address,
        amount
    )

    # This is the only possible path, the transfer must go backwards
    assert_path_mediated_transfer(
        get_sent_transfer(channel_0_3, 0),
        get_sent_transfer(channel_3_2, 0),
    )

    # Now let's query the WAL to see if the state changes were logged as expected
    app0_state_changes = [
        change[1] for change in get_all_state_changes(app0.raiden.transaction_log)
        if not isinstance(change[1], Block)
    ]
    app0_events = [
        event.event_object for event in get_all_state_events(app0.raiden.transaction_log)
    ]
    app1_state_changes = [
        change[1] for change in get_all_state_changes(app1.raiden.transaction_log)
        if not isinstance(change[1], Block)
    ]
    app1_events = [
        event.event_object for event in get_all_state_events(app1.raiden.transaction_log)
    ]
    app2_state_changes = [
        change[1] for change in get_all_state_changes(app2.raiden.transaction_log)
        if not isinstance(change[1], Block)
    ]
    app2_events = [
        event.event_object for event in get_all_state_events(app2.raiden.transaction_log)
    ]
    app3_state_changes = [
        change[1] for change in get_all_state_changes(app3.raiden.transaction_log)
        if not isinstance(change[1], Block)
    ]
    app3_events = [
        event.event_object for event in get_all_state_events(app3.raiden.transaction_log)
    ]

    # app1 received one direct transfers
    assert len(app1_state_changes) == 1
    assert len(app1_events) == 1

    # app0 initiates the direct transfer and mediated_transfer
    assert len(app0_state_changes) == 4
    assert isinstance(app0_state_changes[1], ActionInitInitiator)
    assert app0_state_changes[1].our_address == app0.raiden.address
    assert app0_state_changes[1].transfer.amount == amount
    assert app0_state_changes[1].transfer.token == token_address
    assert app0_state_changes[1].transfer.initiator == app0.raiden.address
    assert app0_state_changes[1].transfer.target == app2.raiden.address
    # The ActionInitInitiator state change does not have the following fields populated.
    # They get populated via an event during the processing of the state change inside
    # this function: mediated_transfer.mediated_transfer.initiator.try_new_route()
    assert app0_state_changes[1].transfer.expiration is None
    assert app0_state_changes[1].transfer.hashlock is None
    assert app0_state_changes[1].transfer.secret is None
    # We should have one available route
    assert len(app0_state_changes[1].routes.available_routes) == 1
    assert len(app0_state_changes[1].routes.ignored_routes) == 0
    assert len(app0_state_changes[1].routes.refunded_routes) == 0
    assert len(app0_state_changes[1].routes.canceled_routes) == 0
    # Of these 2 the state machine will in the future choose the one with the most
    # available balance
    not_taken_route = RouteState(
        state='opened',
        node_address=app1.raiden.address,
        channel_address=channel_0_1.channel_address,
        available_balance=deposit,
        settle_timeout=settle_timeout,
        reveal_timeout=reveal_timeout,
        closed_block=None,
    )
    taken_route = RouteState(
        state='opened',
        node_address=app3.raiden.address,
        channel_address=channel_0_3.channel_address,
        available_balance=deposit,
        settle_timeout=settle_timeout,
        reveal_timeout=reveal_timeout,
        closed_block=None,
    )
    assert taken_route in app0_state_changes[1].routes.available_routes
    assert not_taken_route not in app0_state_changes[1].routes.available_routes

    secret = None
    for event in app0_events:
        if isinstance(event, SendRevealSecret):
            secret = event.secret

    assert secret is not None
    hashlock = sha3(secret)

    assert must_contain_entry(app0_state_changes, ReceiveSecretRequest, {
        'amount': amount,
        'sender': app2.raiden.address,
        'hashlock': hashlock,
    })

    assert must_contain_entry(app0_state_changes, ReceiveSecretReveal, {
        'sender': app3.raiden.address,
        'secret': secret,
    })

    assert must_contain_entry(app0_events, EventTransferSentSuccess, {})

    assert must_contain_entry(app0_events, SendMediatedTransfer, {
        'token': token_address,
        'amount': amount,
        'hashlock': hashlock,
        'initiator': app0.raiden.address,
        'target': app2.raiden.address,
        'receiver': app3.raiden.address,
    })

    assert must_contain_entry(app0_events, SendRevealSecret, {
        'secret': secret,
        'token': token_address,
        'receiver': app2.raiden.address,
        'sender': app0.raiden.address,
    })

    assert must_contain_entry(app0_events, SendBalanceProof, {
        'token': token_address,
        'channel_address': channel_0_3.channel_address,
        'receiver': app3.raiden.address,
        'secret': secret,
    })

    assert must_contain_entry(app0_events, EventTransferSentSuccess, {})
    assert must_contain_entry(app0_events, EventUnlockSuccess, {
        'hashlock': hashlock,
    })

    assert must_contain_entry(app3_state_changes, ActionInitMediator, {
        'our_address': app3.raiden.address,
        'from_route': {
            'state': 'opened',
            'node_address': app0.raiden.address,
            'channel_address': channel_0_3.channel_address,
            'available_balance': deposit,
            'settle_timeout': settle_timeout,
            'reveal_timeout': reveal_timeout,
            'closed_block': None,
        },
        'from_transfer': {
            'amount': amount,
            'hashlock': hashlock,
            'token': token_address,
            'initiator': app0.raiden.address,
            'target': app2.raiden.address,
        }
    })

    assert must_contain_entry(app3_state_changes, ReceiveSecretReveal, {
        'sender': app2.raiden.address,
        'secret': secret,
    })

    assert must_contain_entry(app3_state_changes, ReceiveSecretReveal, {
        'sender': app2.raiden.address,
        'secret': secret,
    })

    assert must_contain_entry(app3_events, SendMediatedTransfer, {
        'token': token_address,
        'amount': amount,
        'hashlock': hashlock,
        'initiator': app0.raiden.address,
        'target': app2.raiden.address,
        'receiver': app2.raiden.address,
    })

    assert must_contain_entry(app3_events, SendRevealSecret, {
        'secret': secret,
        'token': token_address,
        'receiver': app0.raiden.address,
        'sender': app3.raiden.address,
    })

    assert must_contain_entry(app3_events, SendBalanceProof, {
        'token': token_address,
        'channel_address': channel_3_2.channel_address,
        'receiver': app2.raiden.address,
        'secret': secret,
    })
    assert must_contain_entry(app3_events, EventUnlockSuccess, {})

    assert must_contain_entry(app2_state_changes, ActionInitTarget, {
        'our_address': app2.raiden.address,
        'from_route': {
            'state': 'opened',
            'node_address': app3.raiden.address,
            'channel_address': channel_3_2.channel_address,
            'available_balance': deposit,
            'settle_timeout': settle_timeout,
            'reveal_timeout': reveal_timeout,
            'closed_block': None
        },
        'from_transfer': {
            'amount': amount,
            'hashlock': hashlock,
            'token': token_address,
            'initiator': app0.raiden.address,
            'target': app2.raiden.address,
        }
    })

    assert must_contain_entry(app2_state_changes, ReceiveSecretReveal, {
        'sender': app0.raiden.address,
        'secret': secret,
    })

    assert must_contain_entry(app2_state_changes, ReceiveSecretReveal, {
        'sender': app3.raiden.address,
        'secret': secret,
    })

    assert must_contain_entry(app2_events, SendSecretRequest, {
        'amount': amount,
        'hashlock': hashlock,
        'receiver': app0.raiden.address,
    })

    assert must_contain_entry(app2_events, SendRevealSecret, {
        'token': token_address,
        'secret': secret,
        'receiver': app3.raiden.address,
        'sender': app2.raiden.address,
    })
