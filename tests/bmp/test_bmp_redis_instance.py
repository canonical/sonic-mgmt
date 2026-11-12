"""
Test the database intance
"""
import logging
import pytest

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology('any')
]


def test_bmp_redis_instance(duthosts, rand_one_dut_hostname):
    """
    @summary: Test that redis has bmp instance
    """
    duthost = duthosts[rand_one_dut_hostname]
    redis_bmp = duthost.get_process_table("database").get("redis_bmp", bare=False)
    logging.debug("redis_bmp in database: {}".format(redis_bmp))
    assert redis_bmp is not None and redis_bmp.state == "RUNNING", "redis_bmp is not in RUNNING status"
