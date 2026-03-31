from os import environ


environ["TF_CPP_MIN_LOG_LEVEL"] = "2"

import essentia


essentia.log.infoActive = False
essentia.log.warningActive = False

from itertools import chain
from json import load
from sys import argv

import essentia.standard as es
import numpy as np


def process_labels(label):
    return label.replace("---", "/")


def get_genres_per_minute(filename):
    top_n = 5
    json_file = "discogs/discogs-effnet-bs64-1.json"
    model_file = "discogs/discogs-effnet-bs64-1.pb"

    with open(json_file) as f:
        metadata = load(f)

    sample_rate = metadata["inference"]["sample_rate"]
    labels = list(map(process_labels, metadata["classes"]))

    model = es.TensorflowPredictEffnetDiscogs(graphFilename=model_file)

    audio = es.MonoLoader(filename=filename, sampleRate=sample_rate)()

    activations = model(audio)
    activations_mean = np.mean(activations, axis=0)
    top_n_idx = np.argsort(activations_mean)[::-1][:top_n]

    result = {
        "label": list(chain(*[[labels[idx]] * activations.shape[0] for idx in top_n_idx])),
        "activation": list(chain(*[activations[:, idx] for idx in top_n_idx])),
    }

    print(f"File                        : {filename}")
    print(f"Sample Rate                 : {sample_rate / 1000} kHz")
    print(f"Genre/Style (unique)        : {list(set(result['label']))}")
    print(f"Activation Energy (first {top_n * 3}) : {result['activation'][: top_n * 3]}")


get_genres_per_minute(argv[1])
