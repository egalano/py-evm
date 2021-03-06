import asyncio
import os

import pytest

import rlp
from rlp import sedes

from evm.chains.mainnet import BaseMainnetChain
from evm.db.backends.memory import MemoryDB
from evm.rlp.headers import BlockHeader

from p2p.lightchain import LightPeerChain
from p2p.les import (
    LESProtocol,
    Announce,
    BlockHeaders,
    GetBlockHeaders,
    Status,
)
from p2p.peer import LESPeer
from p2p import protocol

from integration_test_helpers import FakeAsyncChainDB
from peer_helpers import (
    get_directly_linked_peers,
    get_fresh_mainnet_headerdb,
)


class MainnetLightPeerChain(BaseMainnetChain, LightPeerChain):
    pass


# A full header sync may involve several round trips, so we must be willing to wait a little bit
# for them.
HEADER_SYNC_TIMEOUT = 3


@pytest.mark.asyncio
async def test_incremental_header_sync(request, event_loop, headerdb_mainnet_100):
    # Here, server will be a peer with a pre-populated headerdb, and we'll use it to send Announce
    # msgs to the client, which will then ask the server for any headers it's missing until their
    # headerdbs are in sync.
    light_chain, _, server = await get_lightchain_with_peers(
        request, event_loop, get_fresh_mainnet_headerdb())

    # We start the client/server with fresh headerdbs above because we don't want them to start
    # syncing straight away -- instead we want to manually trigger incremental syncs by having the
    # server send Announce messages. We're now ready to give our server a populated headerdb.
    server.headerdb = headerdb_mainnet_100

    # The server now announces block #10 as the new head...
    server.send_announce(head_number=10)

    # ... and we wait for the client to process that and request all headers it's missing up to
    # block #10.
    header_10 = server.headerdb.get_canonical_block_header_by_number(10)
    await wait_for_head(light_chain.headerdb, header_10)
    assert_canonical_chains_are_equal(light_chain.headerdb, server.headerdb, 10)

    # Now the server announces block 100 as its current head...
    server.send_announce(head_number=100)

    # ... and the client should then fetch headers from 10-100.
    header_100 = server.headerdb.get_canonical_block_header_by_number(100)
    await wait_for_head(light_chain.headerdb, header_100)
    assert_canonical_chains_are_equal(light_chain.headerdb, server.headerdb, 100)


@pytest.mark.asyncio
async def test_full_header_sync_and_reorg(request, event_loop, headerdb_mainnet_100):
    # Here we create our server with a populated headerdb, so upon startup it will announce its
    # chain head and the client will fetch all headers
    light_chain, _, server = await get_lightchain_with_peers(
        request, event_loop, headerdb_mainnet_100)

    # ... and our client should then fetch all headers.
    head = server.headerdb.get_canonical_head()
    await wait_for_head(light_chain.headerdb, head)
    assert_canonical_chains_are_equal(light_chain.headerdb, server.headerdb, head.block_number)

    head_parent = server.headerdb.get_block_header_by_hash(head.parent_hash)
    difficulty = head.difficulty + 1
    new_head = BlockHeader.from_parent(
        head_parent, head_parent.gas_limit, difficulty=difficulty,
        timestamp=head.timestamp, coinbase=head.coinbase)
    server.headerdb.persist_header(new_head)
    assert server.headerdb.get_canonical_head() == new_head
    server.send_announce(head_number=head.block_number, reorg_depth=1)

    await wait_for_head(light_chain.headerdb, new_head)
    assert_canonical_chains_are_equal(light_chain.headerdb, server.headerdb, new_head.block_number)


@pytest.mark.asyncio
async def test_header_sync_with_multi_peers(request, event_loop, headerdb_mainnet_100):
    # In this test we start with one of our peers announcing block #100, and we sync all
    # headers up to that...
    light_chain, _, server = await get_lightchain_with_peers(
        request, event_loop, headerdb_mainnet_100)

    head = server.headerdb.get_canonical_head()
    await wait_for_head(light_chain.headerdb, head)
    assert_canonical_chains_are_equal(light_chain.headerdb, server.headerdb, head.block_number)

    # Now a second peer comes along and announces block #100 as well, but it's different
    # from the one we already have, so we need to fetch that too. And since it has a higher total
    # difficulty than the current head, it will become our new chain head.
    server2_headerdb = server.headerdb
    head_parent = server2_headerdb.get_block_header_by_hash(head.parent_hash)
    difficulty = head.difficulty + 1
    new_head = BlockHeader.from_parent(
        head_parent, head_parent.gas_limit, difficulty=difficulty,
        timestamp=head.timestamp, coinbase=head.coinbase)
    server2_headerdb.persist_header(new_head)
    assert server2_headerdb.get_canonical_head() == new_head
    client2, server2 = await get_client_and_server_peer_pair(
        request,
        event_loop,
        client_headerdb=light_chain.headerdb,
        server_headerdb=server2_headerdb)

    light_chain.register_peer(client2)
    await wait_for_head(light_chain.headerdb, new_head)
    assert_canonical_chains_are_equal(light_chain.headerdb, server2.headerdb, new_head.block_number)


class LESProtocolServer(LESProtocol):
    _commands = [Status, Announce, BlockHeaders, GetBlockHeaders]

    def send_announce(self, block_hash, block_number, total_difficulty, reorg_depth):
        data = {
            'head_hash': block_hash,
            'head_number': block_number,
            'head_td': total_difficulty,
            'reorg_depth': reorg_depth,
            'params': [],
        }
        header, body = Announce(self.cmd_id_offset).encode(data)
        self.send(header, body)

    def send_block_headers(self, headers, buffer_value, request_id):
        data = {
            'request_id': request_id,
            'headers': headers,
            'buffer_value': buffer_value,
        }
        header, body = BlockHeaders(self.cmd_id_offset).encode(data)
        self.send(header, body)


class LESPeerServer(LESPeer):
    """A LESPeer that can send announcements and responds to GetBlockHeaders msgs.

    Used to test our LESPeer implementation. Tests should call .send_announce(), optionally
    specifying a block number to use as the chain's head and then use the helper function
    wait_for_head() to wait until the client peer has synced all headers up to the announced head.
    """
    conn_idle_timeout = 2
    reply_timeout = 1
    max_headers_fetch = 20
    _supported_sub_protocols = [LESProtocolServer]
    _head_number = None

    @property
    def head_number(self):
        if self._head_number is not None:
            return self._head_number

        return self.headerdb.get_canonical_head().block_number

    def send_announce(self, head_number=None, reorg_depth=0):
        if head_number is not None:
            self._head_number = head_number
        header = self.headerdb.get_canonical_block_header_by_number(self.head_number)
        total_difficulty = self.headerdb.get_score(header.hash)
        self.sub_proto.send_announce(
            header.hash, header.block_number, total_difficulty, reorg_depth)

    def handle_sub_proto_msg(self, cmd: protocol.Command, msg: protocol._DecodedMsgType):
        super().handle_sub_proto_msg(cmd, msg)
        if isinstance(cmd, GetBlockHeaders):
            self.handle_get_block_headers(msg)

    def handle_get_block_headers(self, msg):
        query = msg['query']
        block_number = query.block_number_or_hash
        assert isinstance(block_number, int)  # For now we only support block numbers
        if query.reverse:
            start = max(0, query.block - query.max_headers)
            # Shift our range() limits by 1 because we want to include the requested block number
            # in the list of block numbers.
            block_numbers = reversed(range(start + 1, block_number + 1))
        else:
            end = min(self.head_number + 1, block_number + query.max_headers)
            block_numbers = range(block_number, end)

        headers = tuple(
            self.headerdb.get_canonical_block_header_by_number(i)
            for i in block_numbers
        )
        self.sub_proto.send_block_headers(headers, buffer_value=0, request_id=msg['request_id'])


async def get_client_and_server_peer_pair(request, event_loop, client_headerdb, server_headerdb):
    """Return a client/server peer pair with the given chain DBs.

    The client peer will be an instance of LESPeer, configured with the client_headerdb.

    The server peer will be an instance of LESPeerServer, which is necessary because we want a
    peer that can respond to GetBlockHeaders requests.
    """
    return await get_directly_linked_peers(
        request, event_loop,
        LESPeer, client_headerdb,
        LESPeerServer, server_headerdb)


async def get_lightchain_with_peers(request, event_loop, server_peer_headerdb):
    """Return a MainnetLightPeerChain instance with a client/server peer pair.

    The server is a LESPeerServer instance that can be used to send Announce and BlockHeaders
    messages, and the client will be registered with the LightPeerChain so that a sync
    request is added to the LightPeerChain's queue every time a new Announce message is received.
    """
    headerdb = get_fresh_mainnet_headerdb()
    light_chain = MainnetLightPeerChain(headerdb, MockPeerPool())
    asyncio.ensure_future(light_chain.run())
    await asyncio.sleep(0)  # Yield control to give the LightPeerChain a chance to start

    def finalizer():
        event_loop.run_until_complete(light_chain.cancel())

    request.addfinalizer(finalizer)

    client, server = await get_client_and_server_peer_pair(
        request, event_loop, headerdb, server_peer_headerdb)
    light_chain.register_peer(client)
    return light_chain, client, server


class MockPeerPool:

    def __init__(self, *args, **kwargs):
        pass

    def subscribe(self, subscriber):
        pass

    def unsubscribe(self, subscriber):
        pass

    async def run(self):
        pass

    async def cancel(self):
        pass


def assert_canonical_chains_are_equal(headerdb1, headerdb2, block_number=None):
    """Assert that the canonical chains in both DBs are identical up to block_number."""
    if block_number is None:
        block_number = headerdb1.get_canonical_head().block_number
        assert block_number == headerdb2.get_canonical_head().block_number
    for i in range(0, block_number + 1):
        assert headerdb1.get_canonical_block_header_by_number(i) == (
            headerdb2.get_canonical_block_header_by_number(i))


@pytest.fixture
def headerdb_mainnet_100():
    """Return a headerdb with mainnet headers numbered from 0 to 100."""
    here = os.path.dirname(__file__)
    headers_rlp = open(os.path.join(here, 'fixtures', 'sample_1000_headers_rlp'), 'r+b').read()
    headers = rlp.decode(headers_rlp, sedes=sedes.CountableList(BlockHeader))
    headerdb = FakeAsyncChainDB(MemoryDB())
    for i in range(0, 101):
        headerdb.persist_header(headers[i])
    return headerdb


async def wait_for_head(headerdb, header):
    async def wait_loop():
        while headerdb.get_canonical_head() != header:
            await asyncio.sleep(0.1)
    await asyncio.wait_for(wait_loop(), HEADER_SYNC_TIMEOUT)
