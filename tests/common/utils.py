# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import json
import os
from typing import Any, Dict


def load_test_data_file(file):
    with open(file, "r") as f:
        if file.endswith(".json"):
            return json.load(f)
        else:
            return f.read()


def load_test_data(path) -> Dict[str, Any]:
    jsons_by_path = {}
    with open(os.path.join(path, "responses.json"), "r") as f:
        jsons_by_path = json.load(f)
    data = {}
    for url, value in jsons_by_path.items():
        match value:
            case str():
                filepath = os.path.join(path, value)
                data[url] = load_test_data_file(filepath)
            case list():
                lst = []
                for filename in value:
                    filepath = os.path.join(path, filename)
                    lst.append(load_test_data_file(filepath))
                data[url] = lst
    return data


def read_test_data_file(relative_path: str) -> str:
    path = os.path.join(os.path.dirname(__file__), "../data", relative_path)
    with open(path, "r") as f:
        return f.read()


def read_fixture(filename: str) -> str:
    return read_test_data_file(f"fixtures/{filename}")
