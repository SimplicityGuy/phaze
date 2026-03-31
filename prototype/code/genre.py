from sys import argv

import essentia
import essentia.standard as es
import essentia.streaming


# Load the DiscogsResNet model
model = es.TensorflowPredictMusiCNN(
    graphFilename="discogs/discogs-effnet-bs64-1.pb", input="serving_default_melspectrogram", output="PartitionedCall", batchSize=1
)


# Define a function to extract reported genres for every minute in an MP3 file
def extract_genres(input_file):
    # Create an audio loader
    loader = es.MonoLoader(filename=input_file)

    # Create an audio stream
    audio = loader()

    # Create a framecutter to split the audio into 1-minute chunks
    framecutter = es.FrameCutter(frameSize=60 * 44100, hopSize=60 * 44100)

    # Create a pool to store the genre information for each chunk
    pool = essentia.Pool()

    # Create a loop to process each chunk of audio
    for frame in framecutter(audio):
        # Calculate the mel spectrogram of the chunk
        mel = es.Spectrum()(frame)

        # Reshape the mel spectrogram to fit the input size of the model
        mel_reshaped = mel.reshape(1, mel.shape[0], mel.shape[1])

        # Run the model to extract genre information
        genres = model(mel_reshaped)[0]

        # Add the genre information to the pool
        pool.add("genres", genres)

    # Return the pool as a dictionary
    return pool.descriptorNames(), pool["genres"]


input_file = argv[1]
genres_dict = extract_genres(input_file)
print(genres_dict)
