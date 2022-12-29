import sys; sys.path.insert(0, "../")

import warnings
warnings.simplefilter("ignore", UserWarning)
import torch
import numpy as np
from helper.calibration_gp_utils import gp_calibration_eval_wandb, load_data, load_model

if __name__ == "__main__":
    np.random.seed(7777)
    torch.manual_seed(7777)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = True

    print(torch.has_mps)
    print(torch.cuda.is_available())
    print(torch.cuda.device_count())
    print(torch.cuda.current_device())
    print(torch.cuda.get_device_name(0))


    REPO = "BNN-preds"
    # REPO = "Laplace"

    DATASET = "FMNIST"
    # DATASET = "CIFAR10"

    # SUBSET_OF_WEIGHTS = "last_layer"
    # SUBSET_OF_WEIGHTS = "all"

    for SUBSET_OF_WEIGHTS in ["last_layer", "all"]:
        for DEFAULT_TYPE in [torch.float32, torch.float64]:
            torch.set_default_dtype(DEFAULT_TYPE)

            train_loader, test_loader, ds_train = load_data(REPO, DATASET)
            model = load_model(repo=REPO, dataset=DATASET, train_data=ds_train)

            wandb_kwargs = {
                    'project': 'laplace',
                    'entity': 'metodj',
                    'notes': '',
                    'mode': 'online',
                    'config': {"repo": REPO, "dataset": DATASET,
                               "subset_of_weights": SUBSET_OF_WEIGHTS, "default_type": DEFAULT_TYPE}
                }

            gp_calibration_eval_wandb(model=model, train_loader=train_loader,wandb_kwargs=wandb_kwargs,
                                      subset_of_weights=SUBSET_OF_WEIGHTS, test_loader=test_loader,
                                      M_arr=[50, 100, 200, 400, 800, 1600])