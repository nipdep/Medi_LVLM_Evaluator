from huggingface_hub import hf_hub_download
import torch
import os
from open_flamingo import create_model_and_transforms
from accelerate import Accelerator
from einops import repeat
from PIL import Image
import sys, json
import argparse
sys.path.append('..')
from src.utils import FlamingoProcessor
from demo_utils import image_paths, clean_generation
import pdb
import numpy as np
import difflib


from dataset import DemoTest

import numpy as np
from torch.utils.data import DataLoader  



def str_similarity(str1, str2):
    seq = difflib.SequenceMatcher(None, str1, str2)
    return seq.ratio()

def append_to_json(save_path, old_data, pred_now, pred_now_compact, correct_flag):

    base_name = old_data['question_id'][0] + '.json'
    filename = os.path.join(save_path, base_name)

    old_data['pred'] = pred_now
    old_data['pred_compact'] = pred_now_compact
    old_data['correct'] = correct_flag
    json_data = old_data

    with open(filename, 'w') as f:
        f.write(json.dumps(json_data, indent=4))

def find_most_similar_index(str_list, target_str):
    """
    Given a list of strings and a target string, returns the index of the most similar string in the list.
    """
    # Initialize variables to keep track of the most similar string and its index
    most_similar_str = 'Error Bug'
    most_similar_index = 'Error Bug'
    highest_similarity = 0
    
    # Iterate through each string in the list
    for i, str in enumerate(str_list):
        # Calculate the similarity between the current string and the target string
        similarity = str_similarity(str, target_str)
        
        # If the current string is more similar than the previous most similar string, update the variables
        if similarity > highest_similarity:
            most_similar_str = str
            most_similar_index = i
            highest_similarity = similarity
    
    # Return the index of the most similar string
    return most_similar_str


def get_args_parser():
    parser = argparse.ArgumentParser('med-flamingo test', add_help=False)
    parser.add_argument('--output_dir', default='./output',
                        help='path where to save, empty for no saving')
    parser.add_argument('--json_path', default='json_path',
                        help='a json file that contains the path of all the evaluated json files')
    return parser


def main():

    args = get_args_parser()
    args = args.parse_args()
    save_root_path = os.path.join(args.output_dir, 'saved_dir')

    accelerator = Accelerator() #when using cpu: cpu=True
    device = accelerator.device
    
    print('Loading model..')

    # >>> add your local path to Llama-7B (v1) model here:
    llama_path = 'llama_path'
    if not os.path.exists(llama_path):
        raise ValueError('Llama model not yet set up, please check README for instructions!')

    model, image_processor, tokenizer = create_model_and_transforms(
        clip_vision_encoder_path="ViT-L-14",
        clip_vision_encoder_pretrained="openai",
        lang_encoder_path=llama_path,
        tokenizer_path=llama_path,
        cross_attn_every_n_layers=4
    )
    # load med-flamingo checkpoint:
    checkpoint_path = hf_hub_download("med-flamingo/med-flamingo", "model.pt")
    print(f'Downloaded Med-Flamingo checkpoint to {checkpoint_path}')
    model.load_state_dict(torch.load(checkpoint_path, map_location=device), strict=False)
    processor = FlamingoProcessor(tokenizer, image_processor)

    # go into eval model and prepare:
    model = accelerator.prepare(model)
    is_main_process = accelerator.is_main_process
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.to(device)
    model.eval()
    print('Finish loading model.')

    print('Prepare Data')


    json_pool_path = args.json_path
    with open(json_pool_path, 'r') as json_pool_file:
        json_pool = json.load(json_pool_file)
    dataset_position = os.path.join(args.output_dir, 'dataset_position.npy')
    if os.path.exists(dataset_position):
        begin_dataset = int(np.load(dataset_position)[0])
    else:
        begin_dataset = 0

    for json_iter in range(begin_dataset, len(json_pool)):
        np.save(dataset_position, [json_iter])
        json_path_now = json_pool[json_iter]

        modality_now = json_path_now.split('/')[-3]
        dataset_now = json_path_now.split('/')[-2]
        save_json_path = os.path.join(save_root_path, modality_now, dataset_now)
        if not os.path.exists(save_json_path):
            os.makedirs(save_json_path)


        # setup dataset
        print("Setup Data")
        Test_dataset = DemoTest(json_path_now, processor)
        Test_dataloader = DataLoader(
                Test_dataset,
                batch_size=1,
                num_workers=1,
                pin_memory=True,
                sampler=None,
                shuffle=False,
                collate_fn=None,
                drop_last=False,
        )



        print("Start Testing")

        # setup test record
        test_position = os.path.join(args.output_dir, 'test_position{}.npy'.format(json_iter))
        save_path = os.path.join(args.output_dir, 'test_log{}.txt'.format(json_iter))
        begin_point = os.path.exists(test_position)
        if begin_point:
            test_record = open(save_path, 'a')
            start_point = int(np.load(test_position)[0])
            ACC = int(np.load(test_position)[1])
        else:
            test_record = open(save_path, 'w')
            start_point = 0
            ACC = 0

        print('modality_now: {}, dataset_now: {} \n'.format(modality_now, dataset_now))
        test_record.write('modality_now: {}, dataset_now: {} \n'.format(modality_now, dataset_now))



        for i, sample in enumerate(Test_dataloader):
            if i < start_point or i == start_point:
                continue
            else:
                with torch.no_grad():
                    vision_x = sample['vision_x'].to(device)
                    lang_x = sample['lang_x'][0].to(device)
                    attention_mask = sample['attention_mask'][0].to(device)


                    generated_text = model.generate(
                        vision_x=vision_x,
                        lang_x=lang_x,
                        attention_mask=attention_mask,
                        max_new_tokens=50,
                        )
                    response = processor.tokenizer.decode(generated_text[0])
                    response = clean_generation(response)

                    generated_answer = response.split('### Answer:')[1]
                    generated_answer_compact = generated_answer[:6]
                

                    gt_content = sample['gt_content'][0]
                    Choice_list = [item[0] for item in sample['choice_list']]
                    Choice_list_long = [item[0] for item in sample['choice_list_long']]
                    matched_pred = find_most_similar_index(Choice_list_long, generated_answer_compact)
                    matched_gt  = find_most_similar_index(Choice_list_long, gt_content)
                    corret = 0
                    if matched_pred == matched_gt:
                        ACC = ACC +1
                        corret = 1 

                    append_to_json(save_json_path, sample['info_all'], generated_answer, generated_answer_compact, corret)
                    np.save(test_position, [i, ACC])

                    if i % 100 == 0:
                        accuracy = ACC / i
                        print('The accuracy at {} is {} \n'.format(i, accuracy))
                        test_record.write('The accuracy at {} is {} \n'.format(i, accuracy))
                        test_record.flush()

        final_accuracy = ACC/i
        print('The test is end, we test {} samples, the final accuracy is {} \n'.format(i, final_accuracy))
        test_record.write('The test is end, we test {} samples, the final accuracy is {} \n'.format(i, final_accuracy))
        test_record.flush()





if __name__ == "__main__":

    main()
