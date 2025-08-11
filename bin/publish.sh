#!/bin/bash
poetry lock --no-update
poetry install
poetry build
poetry publish