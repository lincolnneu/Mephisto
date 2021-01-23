#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from flask import Flask, Blueprint, send_file, jsonify, request
import os
import atexit
import signal
import csv
import sys
import time
import threading


def run(
    build_dir,
    port,
    output,
    csv_headers,
    json=False,
    database_task_name=None,
    all_data=False,
    debug=False,
):
    global index_file, app
    global ready_for_next, current_data, finished, index_file
    global counter
    global all_data_list

    RESULTS_PER_PAGE_DEFAULT = 10

    if not debug or output == "":
        # disable noisy logging of flask, https://stackoverflow.com/a/18379764
        import logging

        flask_log = logging.getLogger("werkzeug")
        flask_log.disabled = True
        flask_cli = sys.modules["flask.cli"]
        flask_cli.show_server_banner = lambda *x: None

    app = Flask(
        __name__,
        root_path=os.getcwd(),
        static_url_path="/static",
        static_folder=build_dir + "/static",
    )

    def json_reader(f):
        import json

        for jsonline in f:
            yield json.loads(jsonline)

    def mephistoDBReader():
        from mephisto.abstractions.databases.local_database import LocalMephistoDB
        from mephisto.tools.data_browser import DataBrowser as MephistoDataBrowser

        db = LocalMephistoDB()
        mephisto_data_browser = MephistoDataBrowser(db=db)

        def format_data_for_review(data):
            contents = data["data"]
            return f"{data}"

        units = mephisto_data_browser.get_units_for_task_name(database_task_name)
        for unit in units:
            yield format_data_for_review(mephisto_data_browser.get_data_from_unit(unit))

    def consume_data():
        global ready_for_next, current_data, finished, counter

        if database_task_name is not None:
            data_source = mephistoDBReader()
        elif json:
            data_source = json_reader(iter(sys.stdin.readline, ""))
        else:
            data_source = csv.reader(iter(sys.stdin.readline, ""))
            if csv_headers:
                next(data_source)

        finished = False
        counter = 0
        for row in data_source:
            ready_for_next = threading.Event()
            current_data = row
            counter += 1
            ready_for_next.wait()
        finished = True

    def consume_all_data(page, results_per_page):
        limit = (
            results_per_page
            if (
                results_per_page is not None
                and isinstance(results_per_page, int)
                and results_per_page > 0
            )
            else RESULTS_PER_PAGE_DEFAULT
        )
        paginated = (
            True if page is not None and isinstance(page, int) and page > 0 else False
        )

        first_index = (page - 1) * limit if paginated else 0
        data_point_list = []

        if database_task_name is not None:
            # if reading from MephistoDB
            data_source = mephistoDBReader()

            if paginated:
                for x in range(first_index):
                    data = next(data_source, None)
                    if data is None:
                        return []
                for x in range(limit):
                    data = next(data_source, None)
                    if data is None:
                        break
                    data_point_list.append(data)
            else:
                for row in data_source:
                    data_point_list.append(row)
        else:
            # If reading from a file all data points are referenced from memory
            global all_data_list
            if paginated:
                list_len = len(all_data_list)
                if first_index > list_len - 1:
                    return []
                limit = min(first_index + limit, list_len) - first_index
                if limit < 0:
                    return []
                for x in range(first_index, first_index + limit):
                    data_point_list.append(all_data_list[x])
            else:
                for row in all_data_list:
                    data_point_list.append(row)

        return data_point_list

    @app.route("/data_for_current_task")
    def data():
        global current_data, finished
        if all_data:
            return jsonify(
                {
                    "error": "mephisto review is in all mode, please do not use the --all flag to review individual tasks"
                }
            )
        if finished:
            func = request.environ.get("werkzeug.server.shutdown")
            if func is None:
                raise RuntimeError("Not running with the Werkzeug Server")
            func()

        return jsonify(
            {"finished": finished, "data": current_data if not finished else None}
        )

    @app.route("/all_data_for_current_task")
    def all_task_data():
        if not all_data and database_task_name is None:
            return jsonify(
                {
                    "error": "mephisto review is not in all mode, please use the --all flag"
                }
            )
        page = request.args.get("page", default=None, type=int)
        results_per_page = request.args.get("results_per_page", default=None, type=int)
        data_point_list = consume_all_data(page, results_per_page)
        return jsonify({"data": data_point_list, "length": len(data_point_list)})

    @app.route("/submit_current_task", methods=["GET", "POST"])
    def next_task():
        global current_data, ready_for_next, finished, counter
        if all_data:
            return jsonify(
                {
                    "error": "mephisto review is in all mode, please do not use the --all flag to review individual tasks"
                }
            )
        result = (
            request.get_json(force=True)
            if request.method == "POST"
            else request.ags.get("result")
        )

        if output == "":
            sys.stdout.write("{}\n".format(result))
            sys.stdout.flush()
        else:
            with open(output, "a+") as f:
                f.write("{}\n".format(result))

        ready_for_next.set()
        time.sleep(0)
        return jsonify({"finished": finished, "counter": counter})

    @app.route("/")
    def index():
        global index_file
        return send_file(build_dir + "/index.html")

    @app.after_request
    def after_request(response):
        response.headers.add("Access-Control-Allow-Origin", "*")
        response.headers.add(
            "Access-Control-Allow-Headers", "Content-Type,Authorization"
        )
        response.headers.add(
            "Access-Control-Allow-Methods", "GET,PUT,POST,DELETE,OPTIONS"
        )
        response.headers.add("Cache-Control", "no-store")
        return response

    if all_data and database_task_name is None:
        # if reading all data points from a file, all data is loaded into memory before the app starts
        if json:
            data_source = json_reader(iter(sys.stdin.readline, ""))
        else:
            data_source = csv.reader(iter(sys.stdin.readline, ""))
            if csv_headers:
                next(data_source)

        all_data_list = []

        for row in data_source:
            all_data_list.append(row)

    if not all_data:
        thread = threading.Thread(target=consume_data)
        thread.start()
    if sys.stdout.isatty():
        print("Running on http://127.0.0.1:{}/ (Press CTRL+C to quit)".format(port))
    app.run(debug=False, port=port)
