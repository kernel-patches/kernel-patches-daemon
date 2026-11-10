# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# pyre-unsafe

import asyncio
from typing import Any, Awaitable, List

from git.util import remove_password_if_present


async def gather_or_cancel(*aws: Awaitable[Any]) -> List[Any]:
    """
    Run the awaitables like asyncio.gather(). If one of them raises, cancel
    the others, so that they do not keep sending requests after the caller
    gave up.
    """
    tasks = [asyncio.ensure_future(aw) for aw in aws]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        raise


def redact_url(url: str) -> str:
    """
    A URL may contain username/password. Replace the password so it does not end up in the logs.
    """
    return remove_password_if_present([url])[0]


def remove_unsafe_chars(s: str) -> str:
    """
    Remove "unsafe" characters, i.e., those that we may not want to preserve in file names.
    """
    return "".join([c for c in s if c in ["_", "-"] or (c.isalnum() and c.isascii())])
