import os, re, time, json
import PIL.Image, PIL.ImageFont, PIL.ImageDraw
import numpy as np
import tensorflow as tf
import pandas as pd
from matplotlib import pyplot as plt
from tensorflow.keras.models import load_model
AUTOTUNE = tf.data.AUTOTUNE
print("Tensorflow version " + tf.__version__)

PROJECT = "jg-project-328708" #@param {type:"string"}
BUCKET = "gs://jg-tpubucket"  #@param {type:"string", default:"jddj"}
NEW_MODEL = True #@param {type:"boolean"}
MODEL_NAME = "resnet50" #@param {type:"string"}
MODEL_VERSION = "v1" #@param {type:"string"}

assert PROJECT, 'For this part, you need a GCP project. Head to http://console.cloud.google.com/ and create one.'
assert re.search(r'gs://.+', BUCKET), 'For this part, you need a GCS bucket. Head to http://console.cloud.google.com/storage and create one.'

# detect TPUs
#tpu = tf.distribute.cluster_resolver.TPUClusterResolver.connect('jg-tpu') # TPU detection
#strategy = tf.distribute.TPUStrategy(tpu)
#print("Number of accelerators: ", strategy.num_replicas_in_sync)

# Google TPU VM
cluster_resolver = tf.distribute.cluster_resolver.TPUClusterResolver(tpu='jg-tpu')
tf.config.experimental_connect_to_host(cluster_resolver.master())
tf.tpu.experimental.initialize_tpu_system(cluster_resolver)
tpu_strategy = tf.distribute.experimental.TPUStrategy(cluster_resolver)

def deserialize_image_record(record):
    feature_map = {'image/encoded': tf.io.FixedLenFeature([], tf.string, ''),
                  'image/class/label': tf.io.FixedLenFeature([1], tf.int64, -1),
                  'image/class/text': tf.io.FixedLenFeature([], tf.string, '')}
    obj = tf.io.parse_single_example(serialized=record, features=feature_map)
    imgdata = obj['image/encoded']
    label = tf.cast(obj['image/class/label'], tf.int32)   
    label_text = tf.cast(obj['image/class/text'], tf.string)   
    return imgdata, label, label_text

def val_preprocessing(record):
    imgdata, label, label_text = deserialize_image_record(record)
    label -= 1
    image = tf.io.decode_jpeg(imgdata, channels=3, 
                              fancy_upscaling=False, 
                              dct_method='INTEGER_FAST')

    shape = tf.shape(image)
    height = tf.cast(shape[0], tf.float32)
    width = tf.cast(shape[1], tf.float32)
    side = tf.cast(tf.convert_to_tensor(256, dtype=tf.int32), tf.float32)

    scale = tf.cond(tf.greater(height, width),
                  lambda: side / width,
                  lambda: side / height)
    
    new_height = tf.cast(tf.math.rint(height * scale), tf.int32)
    new_width = tf.cast(tf.math.rint(width * scale), tf.int32)
    
    image = tf.image.resize(image, [new_height, new_width], method='bicubic')
    image = tf.image.resize_with_crop_or_pad(image, 224, 224)
    
    image = models[model_type].preprocess_input(image)
    
    return image, label, label_text

def get_dataset(batch_size, use_cache=False):
    data_dir = '/home/kmubigdatagcp/datasets/images-1000/*'
    files = tf.io.gfile.glob(os.path.join(data_dir))
    dataset = tf.data.TFRecordDataset(files)
    
    dataset = dataset.map(map_func=val_preprocessing, num_parallel_calls=tf.data.experimental.AUTOTUNE)
    dataset = dataset.batch(batch_size=batch_size)
    dataset = dataset.prefetch(buffer_size=tf.data.experimental.AUTOTUNE)
    dataset = dataset.repeat(count=1)
    
    if use_cache:
        shutil.rmtree('tfdatacache', ignore_errors=True)
        os.mkdir('tfdatacache')
        dataset = dataset.cache(f'./tfdatacache/imagenet_val')
    
    return dataset


def tpu_inference(tpu_saved_model_name, batch_size):

    model_tpu = load_model(tpu_saved_model_name)

    first_iter_time = 0
    iter_times = []
    pred_labels = []
    actual_labels = []
    display_threshold = 0

    ds = get_dataset(batch_size)

    ds_iter = ds.make_initializable_iterator()
    ds_next = ds_iter.get_next()
    ds_init_op = ds_iter.initializer

    with tf.Session() as sess:
        try:
            sess.run(ds_init_op)
            counter = 0
            
            total_datas = 1000
            display_every = 100
            display_threshold = display_every
            
            ipname = list(model_inf1.feed_tensors.keys())[0]
            resname = list(model_inf1.fetch_tensors.keys())[0]
            
            walltime_start = time.time()
            extend_time = []
            while True:
                sess_start = time.time()
                (validation_ds,batch_labels,_) = sess.run(ds_next)
                
                model_feed_dict={ipname: validation_ds}
                start_time =time.time()
                inf1_results = model_tpu(model_feed_dict)
                if counter == 0:
                    first_iter_time = time.time() - start_time
                else:
                    iter_times.append(time.time() - start_time)
                
                actual_labels.extend(label for label_list in batch_labels for label in label_list)
                pred_labels.extend(list(np.argmax(inf1_results[resname], axis=1)))
            
                counter+=1
                break
        except tf.errors.OutOfRangeError:
            pass
    print(actual_labels)
    print(pred_labels)
    acc_inf1 = np.sum(np.array(actual_labels) == np.array(pred_labels))/len(actual_labels)
    iter_times = np.array(iter_times)
    
    results = pd.DataFrame(columns = [f'tpu_{batch_size}'])
    results.loc['batch_size']              = [batch_size]
    results.loc['accuracy']                = [acc_inf1]
    results.loc['first_prediction_time']   = [first_iter_time]
    results.loc['average_prediction_time'] = [np.mean(iter_times)]
    results.loc['wall_time']               = [time.time() - walltime_start]
    display(results.T)

    return results, iter_times

batch_list = [1]
model_type = 'resnet50'

tpu_model = f'{model_type}_saved_model'

for batch_size in batch_list:
  opt = {'batch_size': batch_size}
  iter_ds = pd.DataFrame()
  results = pd.DataFrame()
  res, iter_times = tpu_inference(tpu_model, batch_size)

  iter_ds = pd.concat([iter_ds, pd.DataFrame(iter_times, columns=[col_name(opt)])], axis=1)
  results = pd.concat([results, res], axis=1)

display(results)


